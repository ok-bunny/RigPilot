"use strict";

const state = {
  token: sessionStorage.getItem("rigpilot_token") || "",
  user: null,
  agents: [],
  summary: { total: 0, online: 0, offline: 0, normal_mining: 0, alerts: 0, active_commands: 0 },
  commands: [],
  hashrateHistory: null,
  hashrateHistoryError: "",
  hashrateHistoryFetchedAt: 0,
  workload: null,
  workloadError: "",
  workloadLoading: false,
  workloadHasRequested: false,
  workloadRequestId: 0,
  workloadLastRequestAt: 0,
  workloadPreferences: { mode: "multiple", coins: null, weights: {} },
  workloadPreferencesUser: "",
  workloadPreferencesStorageError: false,
  workloadRevenueAmount: "",
  workloadOwners: { initialized: false, revision: 0, groups: [] },
  workloadOwnersUser: "",
  workloadOwnersLoaded: false,
  workloadOwnersBusy: "",
  workloadOwnersRequestId: 0,
  workloadOwnersLegacy: null,
  workloadOwnerDraft: null,
  workloadOwnerError: "",
  settlementNote: { settlement_at: null, legacy_note: null, updated_at: null, updated_by: null, revision: 0 },
  settlementNoteLoaded: false,
  settlementNoteError: "",
  settlementNoteNotice: "",
  settlementNoteDirty: false,
  settlementNoteSaving: false,
  users: [],
  passwordChangeRequest: null,
  setupState: null, setupLoading: false, setupSaving: false, setupRequestId: 0, setupError: "",
  wallets: [],
  pools: [],
  minerPackages: [],
  minerPackageLimits: null,
  agentReleases: [],
  agentUpdates: [],
  agentUpdateRequests: {},
  agentUpdateRequestSequence: 0,
  agentUpdateDismissals: {},
  managedMiners: [],
  minerAdapters: [],
  flightSheets: [],
  page: "dashboard",
  filter: "all",
  overviewOsFilter: "all",
  selectedAgent: null,
  selectedAction: null,
  selectedDeployType: null,
  deviceLog: null,
  deviceLogRequestId: 0,
  deviceHashrateHistory: null,
  deviceHashrateRequestId: 0,
  deviceDetailTab: "gpus",
  disclosureStates: new Map(),
  miningResidueRepair: null,
  miningResidueRepairToken: 0,
  minerCacheCleanup: null,
  minerCacheCleanupToken: 0,
  liveMinerLog: null,
  liveMinerLogToken: 0,
  deviceGpuOcTarget: null,
  deviceGpuOcBatchTarget: null,
  miningControlBatchAction: "",
  configurationRefreshDismissals: {},
  editingWalletId: null,
  editingPoolId: null,
  editingFlightSheetId: null,
  flightMigrationSourceId: null,
  flightHistoricalReadOnly: false,
  flightMinerConfigPrefix: null,
  flightEditorReturnTop: 0,
  refreshing: false,
  refreshId: 0,
  refreshController: null,
  authEpoch: 0,
  enrollmentRequestId: 0,
  selectedAgentTelemetry: [],
  selectedAgentTelemetryId: null,
  deviceTelemetryRequestId: 0,
};

const API_TIMEOUT_MS = 20_000;
const LARGE_UPLOAD_TIMEOUT_MS = 15 * 60_000;
const WORKLOAD_SETTLEMENT_NOTE_PATH = "/api/workload-settlement-note";
const MINING_RESIDUE_REPAIR_CAPABILITY = "managed_mining.residue_repair.v1";
const MINER_CACHE_CLEANUP_CAPABILITY = "managed_mining.cache_cleanup.v1";
const MINER_CACHE_SCAN_ACTION = "scan_miner_cache.v1";
const MINER_CACHE_CLEANUP_ACTION = "cleanup_miner_cache.v1";
const MINING_RESIDUE_SELECTIVE_CAPABILITY = "managed_mining.residue_repair.selective.v1";
const MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY = "managed_mining.residue_repair.explicit_candidate.v1";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// Semantic keys stay stable through telemetry updates, sorting and filtering.
function disclosureKey(...identity) {
  return JSON.stringify([state.authEpoch, ...identity.map((part) => String(part ?? ""))]);
}

function rememberDisclosures(root) {
  if (!root) return;
  for (const details of root.querySelectorAll("details[data-disclosure-key]")) {
    state.disclosureStates.set(details.dataset.disclosureKey, details.open);
  }
}

function restoreDisclosures(root) {
  if (!root) return;
  for (const details of root.querySelectorAll("details[data-disclosure-key]")) {
    const key = details.dataset.disclosureKey;
    if (state.disclosureStates.has(key)) details.open = state.disclosureStates.get(key);
    details.addEventListener("toggle", () => {
      // Native toggle events can arrive after this node has been replaced.
      if (details.isConnected) state.disclosureStates.set(key, details.open);
    });
  }
}

function themeColor(name, fallback) {
  return window.getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

const actionNames = {
  collect_diagnostics: "采集诊断信息",
  collect_miner_logs: "采集矿工日志",
  stop_mining: "停止挖矿",
  restart_mining: "重启挖矿",
  restart_service: "重启服务",
  reboot: "重启主机",
  shutdown: "关闭主机",
  apply_flight_sheet: "应用飞行表",
  remove_flight_sheet: "去除飞行表",
  "scan_mining_residue.v1": "扫描矿工残留",
  "cleanup_mining_residue.v1": "清理矿工残留",
  "scan_miner_cache.v1": "扫描旧矿工缓存",
  "cleanup_miner_cache.v1": "清理旧矿工缓存",
  configure_mining_autostart: "设置开机自启挖矿",
  apply_gpu_profile: "应用 GPU OC 配置",
  "agent_update.v1": "更新被控端",
  "collect_update_forensics.v1": "更新失败取证",
};

// Actions an operator may cancel while the Agent is already executing them
// (server-side ledger close; the device-side thread is not interrupted).
const RUNTIME_CANCELLABLE_ACTIONS = new Set(["apply_gpu_profile"]);

// NVIDIA's product specification fixes the memory generation for these
// models even when Linux NVML does not expose the DRAM maker.
const gpuModelMemoryTypes = {
  "rtx 5060 ti": "GDDR7",
  "rtx 5080": "GDDR7",
};

const gpuOcFields = [
  "power_limit_w",
  "core_clock_offset_mhz",
  "core_clock_lock_mhz",
  "memory_clock_offset_mhz",
  "memory_clock_lock_mhz",
  "fan_percent",
];

const historicalAgentReleaseNotes = {
  "Linux 0.6.8: repaired independent updater readiness and Linux local/Spark Shell/SFTP access": "Linux 0.6.8：修复独立更新器就绪检测，并增加 Linux 本地/Spark Shell 与 SFTP 访问",
  "Agent 0.6.7: all-device IP reporting, update diagnostics, Linux-compatible controller fields": "Agent 0.6.7：全设备 IP 上报、更新诊断及 Linux 兼容控制端字段",
};

const auditNames = {
  bootstrap_admin: "初始化管理员",
  login_succeeded: "登录成功",
  login_failed: "登录失败",
  enrollment_token_created: "创建接入码",
  agent_enrolled: "设备接入",
  agent_updated: "更新设备",
  agent_deleted: "删除设备",
  command_queued: "下发任务",
  command_running: "任务开始",
  command_succeeded: "任务成功",
  command_failed: "任务失败",
  command_cancelled: "取消任务",
  command_abandoned: "结束遗留任务",
  wallet_created: "创建钱包",
  wallet_updated: "修改钱包",
  wallet_deleted: "删除钱包",
  miner_package_created: "登记矿工软件包",
  miner_package_deleted: "删除矿工软件包",
  flight_sheet_created: "创建飞行表",
  flight_sheet_updated: "修改飞行表",
  flight_sheet_deleted: "删除飞行表",
  configuration_reapply: "自动重下发配置",
  gpu_profile_created: "创建 GPU 配置",
  gpu_profile_deleted: "删除 GPU 配置",
  user_created: "创建用户",
  user_disabled: "停用用户",
  user_deleted: "删除用户",
  miner_package_uploaded: "上传矿工软件包",
  miner_package_imported: "导入矿工软件包",
  agent_release_created: "上传被控端版本",
  agent_release_deleted: "删除被控端版本",
  agent_update_queued: "创建被控端更新",
  agent_update_download_started: "下载被控端更新",
  agent_update_activating: "激活被控端更新",
  agent_update_target_connected: "新版被控端已回连",
  agent_update_succeeded: "被控端更新成功",
  agent_update_failed: "被控端更新失败",
  agent_update_rollback_reported: "被控端已确认回滚",
  agent_update_replacement_stalled: "被控端更新疑似卡滞",
  agent_update_forensics_queued: "排队更新失败取证",
  agent_update_cancelled: "取消被控端更新",
  hashrate_spike_rejected: "拒绝异常算力样本",
  remote_session_created: "创建远程会话",
  remote_session_opened: "远程会话已连接",
  remote_session_closed: "关闭远程会话",
  remote_session_failed: "远程会话失败",
  remote_session_expired: "远程会话已过期",
  nvidia_driver_inspection_queued: "查询 NVIDIA 驱动更新",
  nvidia_driver_update_queued: "创建 NVIDIA 驱动更新",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const requestOptions = { ...options };
  const timeoutMs = Number.isFinite(Number(requestOptions.timeoutMs))
    ? Math.max(0, Number(requestOptions.timeoutMs))
    : API_TIMEOUT_MS;
  delete requestOptions.timeoutMs;
  const externalSignal = requestOptions.signal;
  const controller = new AbortController();
  const abortFromExternal = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromExternal();
  else externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  const timeoutId = timeoutMs > 0
    ? window.setTimeout(() => controller.abort(new DOMException("Request timed out", "TimeoutError")), timeoutMs)
    : 0;
  const tokenAtRequest = state.token;
  const headers = { Accept: "application/json", ...(requestOptions.headers || {}) };
  if (tokenAtRequest) headers.Authorization = `Bearer ${tokenAtRequest}`;
  const bodyIsBinary = requestOptions.body instanceof Blob
    || requestOptions.body instanceof ArrayBuffer
    || ArrayBuffer.isView(requestOptions.body)
    || requestOptions.body instanceof FormData;
  if (requestOptions.body && typeof requestOptions.body !== "string" && !bodyIsBinary) {
    headers["Content-Type"] = "application/json";
    requestOptions.body = JSON.stringify(requestOptions.body);
  }
  try {
    const response = await fetch(path, { ...requestOptions, headers, signal: controller.signal });
    let data = null;
    const type = response.headers.get("content-type") || "";
    if (type.includes("application/json")) data = await response.json();
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/auth/login" && state.token === tokenAtRequest) {
        const passwordChangePending = Boolean(state.passwordChangeRequest);
        logout(false);
        if (passwordChangePending) $("#loginError").textContent = "登录已失效；若刚修改密码，请使用新密码登录。";
      }
      const detail = typeof data?.detail === "string"
        ? data.detail
        : Array.isArray(data?.detail)
          ? data.detail.map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg || "输入无效"}`).join("；")
          : `请求失败 (${response.status})`;
      const responseError = new Error(detail);
      responseError.status = response.status;
      throw responseError;
    }
    return data;
  } catch (error) {
    if (controller.signal.aborted && !externalSignal?.aborted && timeoutMs > 0) {
      const timeoutError = new Error(`请求超时（${Math.ceil(timeoutMs / 1000)} 秒）`);
      timeoutError.name = "TimeoutError";
      throw timeoutError;
    }
    throw error;
  } finally {
    if (timeoutId) window.clearTimeout(timeoutId);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}

function toast(message, kind = "success") {
  const element = document.createElement("div");
  element.className = `toast ${kind === "error" ? "error" : ""}`;
  element.textContent = message;
  $("#toastRegion").append(element);
  setTimeout(() => element.remove(), 3600);
}

function roleLabel(role) {
  return { admin: "管理员", operator: "运维人员", viewer: "查看者" }[role] || role;
}

function osLabel(agent) {
  const os = (agent.os_family || "").toLowerCase();
  if (os.includes("win")) return { short: "WIN", className: "windows" };
  if (os.includes("linux") || os.includes("ubuntu")) return { short: "LIN", className: "ubuntu" };
  return { short: os.slice(0, 3).toUpperCase() || "SYS", className: "" };
}

function statusLabel(status) {
  return { online: "在线", offline: "离线", disabled: "已停用" }[status] || status;
}

function commandStatus(status) {
  return {
    queued: "等待设备", dispatched: "已送达", running: "执行中", succeeded: "成功",
    failed: "失败", cancelled: "已取消", expired: "已过期",
  }[status] || status;
}

function commandErrorMessage(command) {
  const error = String(command?.error || "");
  const profileMatch = error.match(/^(primary|secondary)\.miner_profile is not in the local allowlist(?:; allowed values: (.+))?$/);
  if (profileMatch) {
    const role = profileMatch[1] === "primary" ? "主挖" : "副挖";
    const translated = profileMatch[2]
      ? `${role}本地模板不允许；该设备允许值：${profileMatch[2]}`
      : `${role}本地模板不允许；Windows 便携版请填写 default，自定义版本请查看受控端 miner_profile_allowlist`;
    return `${translated}；原始错误：${error}`;
  }
  if (error.startsWith("temporary SRBMiner GPU OC reset process did not become ready:")) {
    return `SRBMiner 临时 OC 复位进程未能完成就绪确认；临时进程已清理，原飞行表未被替换。请检查矿工日志、显卡驱动和管理员权限后重试；原始错误：${error}`;
  }

  const knownAgentErrors = {
    "previous managed miner exited or restarted itself, but no unique exact replacement could be verified; refusing to start a duplicate miner (inspect/stop the miner manually or reboot the host)": "检测到先前由 Agent 管理的矿工已退出或自行重启，但无法唯一确认替代进程；为避免重复启动，本次已拒绝启动。请手动检查并停止残留矿工进程，或重启主机",
    "previous managed miner API is still listening; refusing to start a duplicate miner": "旧矿工的本地 API 仍在监听，说明矿工或其监管进程可能仍存活；为避免重复挖矿，本次未启动新进程",
    "previous managed miner identity became ambiguous during orphan confirmation; refusing to start a duplicate miner": "自动核对残留期间出现了身份不明确的同名矿工；系统没有结束该进程，也没有启动新的矿工",
    "managed miner process state changed during orphan cleanup": "自动核对残留期间运行记录发生变化；本次未继续启动，请重试原操作",
    "previous managed miner PID creation time no longer matches; refusing to start a duplicate miner": "旧运行记录中的 PID 已被其他创建时间的进程占用；为避免误杀进程或重复挖矿，本次已拒绝启动。请在设备详情中使用“修复挖矿状态”",
    "managed miner process state changed during preboot cleanup": "清理上次开机留下的矿工记录时状态发生变化；请在设备详情中重新执行“修复挖矿状态”",
    "managed miner residue cleanup was blocked by ambiguous or changed process identity": "矿工身份在扫描后发生变化或仍不明确；系统没有结束任何不确定进程，请重新扫描",
    "SRBMiner did not report any GPU devices": "SRBMiner 没有报告任何可用的 GPU，请检查显卡驱动、设备筛选和矿工权限",
    "SRBMiner GPU discovery failed": "SRBMiner GPU 探测失败，请检查显卡驱动、矿工包及执行权限",
    "managed miner exited during startup": "受管矿工在启动过程中退出，请查看矿工日志确认参数或驱动错误",
    "managed miner could not be started": "受管矿工无法启动，请检查矿工包、启动参数和系统权限",
    "managed mining is disabled by local configuration": "被控端本地配置已禁用受管挖矿",
    "managed miner requires an installed package": "飞行表使用的矿工软件包尚未安装到被控端",
    "managed miner package was not installed": "飞行表使用的矿工软件包未成功安装到被控端",
    "installed managed miner package is missing": "被控端已登记的矿工软件包文件缺失",
    "active flight sheet is not configured": "被控端尚未配置活动飞行表",
    "active flight sheet does not exist": "被控端的活动飞行表文件不存在",
    "active flight sheet is invalid": "被控端的活动飞行表内容无效",
    "no NVIDIA PCI display or compute device was found": "没有检测到 NVIDIA PCI 显示或计算设备",
    "Ubuntu did not report a supported NVIDIA driver package for this hardware": "Ubuntu 没有为本机硬件报告受支持的 NVIDIA 驱动包",
    "Ubuntu APT has no installable candidate for the supported NVIDIA driver": "本机可信软件源中没有可安装的 NVIDIA 驱动版本",
    "the NVIDIA driver plan changed; query supported drivers again": "本机可信软件源的最新驱动计划已经变化，请重新查询后再更新",
    "the requested NVIDIA driver plan was not produced by the latest local query": "提交的驱动计划不是设备最近一次本地查询结果，请重新查询",
    "driver downgrade is never allowed": "不允许通过在线更新降级 NVIDIA 驱动",
    "the supported NVIDIA driver is already current": "当前加载的 NVIDIA 驱动已经是本机可信软件源最新可安装版本",
    "another NVIDIA driver operation is already running": "设备上已有 NVIDIA 驱动后台事务正在运行",
    "the NVIDIA driver plan changed after the final APT refresh; query again": "APT 刷新后最新可安装驱动发生变化，请重新查询",
    "the NVIDIA driver update is no longer applicable": "该 NVIDIA 驱动更新计划已不再适用，请重新查询",
    "the expected NVIDIA driver was not loaded after reboot": "重启后没有加载预期的 NVIDIA 驱动，请检查内核模块与 Secure Boot 状态",
  };
  const translated = knownAgentErrors[error];
  return translated ? `${translated}；原始错误：${error}` : error;
}

function metric(value, suffix = "%") {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `${Math.round(number)}${suffix}` : "—";
}

function clampPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.max(0, Math.min(100, number)) : 0;
}

function cpuTemperatureC(metrics) {
  const source = metrics && typeof metrics === "object" ? metrics : {};
  const cpu = source.cpu && typeof source.cpu === "object" ? source.cpu : {};
  const value = cpu.temperature_c ?? source.cpu_temperature_c;
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  // Ignore corrupt sensor values instead of presenting a misleading 0 °C or
  // an impossible temperature. The range also leaves room for sub-zero rigs.
  return Number.isFinite(number) && number >= -40 && number <= 150 ? number : null;
}

function cpuTemperatureLabel(metrics, missingLabel = "") {
  const temperature = cpuTemperatureC(metrics);
  return temperature == null ? missingLabel : `${Math.round(temperature)}°C`;
}

function formatTime(value, relative = true) {
  if (!value) return "从未";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  if (!relative) return date.toLocaleString("zh-CN", { hour12: false });
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 10) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return date.toLocaleDateString("zh-CN");
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${amount >= 10 ? amount.toFixed(1) : amount.toFixed(2)} ${unit}`;
}

function loginView(show) {
  $("#loginView").classList.toggle("hidden", !show);
  $("#appView").classList.toggle("hidden", show);
}

async function login(event) {
  event.preventDefault();
  const error = $("#loginError");
  const button = $("#loginForm button[type=submit]");
  if (button.disabled) return;
  error.textContent = "";
  button.disabled = true;
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: { username: $("#username").value.trim(), password: $("#password").value },
    });
    $("#password").value = "";
    state.authEpoch += 1;
    state.token = result.token;
    sessionStorage.setItem("rigpilot_token", result.token);
    state.user = result.user;
    await enterApp();
  } catch (requestError) {
    error.textContent = requestError.message === "Invalid credentials" ? "用户名或密码错误" : requestError.message;
  } finally {
    button.disabled = false;
  }
}

function logout(showNotice = true) {
  stopLiveMinerLogs();
  state.authEpoch += 1;
  state.refreshId += 1;
  state.refreshController?.abort();
  sessionStorage.removeItem("rigpilot_token");
  Object.assign(state, {
    token: "", user: null, agents: [], commands: [], users: [], passwordChangeRequest: null,
    setupState: null, setupLoading: false, setupSaving: false, setupRequestId: (state.setupRequestId || 0) + 1, setupError: "",
    hashrateHistory: null, hashrateHistoryError: "",
    workload: null, workloadError: "", workloadLoading: false,
    workloadHasRequested: false, workloadRequestId: 0, workloadLastRequestAt: 0,
    workloadPreferences: { mode: "multiple", coins: null, weights: {} }, workloadPreferencesUser: "",
    workloadPreferencesStorageError: false, workloadRevenueAmount: "",
    workloadOwners: { initialized: false, revision: 0, groups: [] }, workloadOwnersUser: "",
    workloadOwnersLoaded: false, workloadOwnersBusy: "", workloadOwnersRequestId: state.workloadOwnersRequestId + 1, workloadOwnersLegacy: null,
    workloadOwnerDraft: null, workloadOwnerError: "",
    settlementNote: { settlement_at: null, legacy_note: null, updated_at: null, updated_by: null, revision: 0 },
    settlementNoteLoaded: false, settlementNoteError: "", settlementNoteNotice: "",
    settlementNoteDirty: false, settlementNoteSaving: false,
    wallets: [], pools: [], minerPackages: [], minerPackageLimits: null, agentReleases: [], agentUpdates: [], agentUpdateRequests: {}, agentUpdateRequestSequence: state.agentUpdateRequestSequence + 1, managedMiners: [], minerAdapters: [], flightSheets: [],
    summary: { total: 0, online: 0, offline: 0, alerts: 0, active_commands: 0 },
    selectedAgent: null, selectedAction: null, selectedDeployType: null,
    deviceLog: null, deviceLogRequestId: state.deviceLogRequestId + 1, deviceHashrateHistory: null, deviceHashrateRequestId: state.deviceHashrateRequestId + 1, deviceDetailTab: "gpus",
    liveMinerLog: null, liveMinerLogToken: 0,
    miningResidueRepair: null, miningResidueRepairToken: state.miningResidueRepairToken + 1,
    minerCacheCleanup: null, minerCacheCleanupToken: state.minerCacheCleanupToken + 1,
    deviceGpuOcTarget: null,
    deviceGpuOcBatchTarget: null,
    configurationRefreshDismissals: {}, agentUpdateDismissals: {}, disclosureStates: new Map(),
    editingWalletId: null, editingPoolId: null, editingFlightSheetId: null,
    flightMigrationSourceId: null, flightHistoricalReadOnly: false, flightMinerConfigPrefix: null,
    filter: "all", refreshing: false, refreshController: null,
    selectedAgentTelemetry: [], selectedAgentTelemetryId: null,
  });
  $$('dialog[open]').forEach((dialog) => dialog.close());
  setWorkloadPreset(24, false);
  closeDrawer();
  $("#enrollmentToken").textContent = "";
  $("#agentCommand").textContent = "";
  $("#serviceName").value = "";
  $("#confirmName").value = "";
  $("#password").value = "";
  for (const selector of ["#passwordChangeCurrent", "#passwordChangeNew", "#passwordChangeConfirm"]) {
    const input = $(selector);
    if (input) input.value = "";
  }
  renderAll();
  loginView(true);
  if (showNotice) $("#loginError").textContent = "你已安全退出。";
}

async function enterApp() {
  if (!state.user) state.user = await api("/api/auth/me");
  loadWorkloadPreferences();
  loadWorkloadOwners();
  $("#currentUsername").textContent = state.user.username;
  $("#currentRole").textContent = roleLabel(state.user.role);
  $("#userAvatar").textContent = state.user.username.slice(0, 1).toUpperCase();
  $$(".admin-only").forEach((element) => element.classList.toggle("hidden", state.user.role !== "admin"));
  $$(".operator-only").forEach((element) => element.classList.toggle("hidden", state.user.role === "viewer"));
  showPage("dashboard");
  const loaded = await refreshAll();
  if (!loaded) {
    logout(false);
    throw new Error("控制台数据加载失败，请检查服务器后重试");
  }
  loginView(false);
  if (state.user?.role === "admin") await loadSetupState({ autoOpen: true });
}

async function refreshAll(silent = false) {
  if (state.refreshing || !state.token) return false;
  const refreshId = ++state.refreshId;
  const authEpoch = state.authEpoch;
  const tokenAtStart = state.token;
  const controller = new AbortController();
  state.refreshing = true;
  state.refreshController = controller;
  $("#refreshButton").classList.add("spinning");
  $("#refreshButton").setAttribute("aria-busy", "true");
  try {
    const stillCurrent = () => refreshId === state.refreshId
      && authEpoch === state.authEpoch
      && tokenAtStart === state.token
      && !controller.signal.aborted;
    const settled = (key, promise) => promise
      .then((data) => ({ key, ok: true, data }))
      .catch((error) => ({ key, ok: false, error }));
    const request = (key, path) => settled(key, api(path, { signal: controller.signal }));
    const hashrateHistoryDue = !silent
      || !state.hashrateHistoryFetchedAt
      || Date.now() - state.hashrateHistoryFetchedAt >= 60000;
    const hashrateHistoryRequest = hashrateHistoryDue
      ? request("hashrateHistory", "/api/hashrate-history?hours=24&bucket_minutes=5")
      : Promise.resolve({ key: "hashrateHistory", ok: true, data: state.hashrateHistory, cached: true });
    const selectedAgentId = state.selectedAgent?.id || null;
    const telemetryRequestId = selectedAgentId ? ++state.deviceTelemetryRequestId : null;
    const secondaryRequests = [
      request("commands", "/api/commands?limit=100&include_all_active=true"),
      hashrateHistoryRequest,
      request("wallets", "/api/wallets"),
      request("pools", "/api/pools"),
      request("minerPackages", "/api/miner-packages"),
      request("minerPackageLimits", "/api/miner-packages/limits"),
      state.user?.role === "admin"
        ? request("agentReleases", "/api/agent-releases")
        : Promise.resolve({ key: "agentReleases", ok: true, data: [] }),
      state.user?.role === "admin"
        ? request("agentUpdates", "/api/agent-updates?limit=100")
        : Promise.resolve({ key: "agentUpdates", ok: true, data: [] }),
      request("managedMiners", "/api/managed-miners"),
      request("minerAdapters", "/api/miner-adapters"),
      request("flightSheets", "/api/flight-sheets"),
      request("settlementNote", WORKLOAD_SETTLEMENT_NOTE_PATH),
      state.user?.role === "admin"
        ? request("users", "/api/users")
        : Promise.resolve({ key: "users", ok: true, data: [] }),
    ];
    if (selectedAgentId) {
      secondaryRequests.push(request(
        "selectedAgentTelemetry",
        `/api/agents/${encodeURIComponent(selectedAgentId)}/telemetry?limit=60`,
      ));
    }
    const secondaryPromise = Promise.all(secondaryRequests);
    const [summaryResult, agentsResult] = await Promise.all([
      request("summary", "/api/summary"),
      request("agents", "/api/agents"),
    ]);
    if (!stillCurrent()) return false;
    if (summaryResult.ok) state.summary = summaryResult.data;
    else console.warn("Summary refresh failed", summaryResult.error);
    if (!agentsResult.ok) {
      if (!silent) toast(agentsResult.error?.message || "设备列表刷新失败", "error");
      return false;
    }
    const agents = Array.isArray(agentsResult.data) ? agentsResult.data : [];
    state.agents = agents;
    if (state.selectedAgent) {
      const updated = agents.find((agent) => agent.id === state.selectedAgent.id) || null;
      if (updated) state.selectedAgent = updated;
      else closeDrawer();
    }
    renderAll();
    $("#lastRefresh").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
    refreshSelectedAgentDrawer(true);

    const secondaryResults = await secondaryPromise;
    if (!stillCurrent()) return false;
    const failed = [];
    for (const result of secondaryResults) {
      if (!result.ok) {
        if (result.key === "minerPackageLimits") state.minerPackageLimits = null;
        if (result.key === "settlementNote") {
          state.settlementNoteLoaded = false;
          state.settlementNoteError = result.error?.message || "结算时间读取失败";
        }
        failed.push(result);
        continue;
      }
      if (result.key === "hashrateHistory") {
        state.hashrateHistory = result.data;
        state.hashrateHistoryError = "";
        if (!result.cached) state.hashrateHistoryFetchedAt = Date.now();
      } else if (result.key === "selectedAgentTelemetry") {
        if (selectedAgentId && state.selectedAgent?.id === selectedAgentId
            && telemetryRequestId === state.deviceTelemetryRequestId) {
          state.selectedAgentTelemetry = Array.isArray(result.data) ? result.data : [];
          state.selectedAgentTelemetryId = selectedAgentId;
        }
      } else if (result.key === "settlementNote") {
        if (!state.settlementNoteLoaded) state.settlementNoteError = "";
        state.settlementNoteLoaded = true;
        if (!state.settlementNoteDirty && !state.settlementNoteSaving) {
          const incoming = result.data && typeof result.data === "object"
            ? result.data
            : { settlement_at: null, legacy_note: null, updated_at: null, updated_by: null, revision: 0 };
          const incomingRevision = Number(incoming.revision) || 0;
          const currentRevision = Number(state.settlementNote?.revision) || 0;
          if (incomingRevision >= currentRevision) state.settlementNote = incoming;
        }
      } else {
        state[result.key] = result.data;
      }
    }
    const hashrateFailure = failed.find((result) => result.key === "hashrateHistory");
    if (hashrateFailure) state.hashrateHistoryError = hashrateFailure.error?.message || "算力历史读取失败";
    for (const result of failed) console.warn(`${result.key} refresh failed`, result.error);
    renderAll();
    refreshSelectedAgentDrawer(true);
    if (workloadRefreshDue()) refreshWorkloadSummary();
    if (!silent && failed.length) toast(`部分数据刷新失败（${failed.length} 项），设备状态已更新`, "error");
    return true;
  } catch (error) {
    if (refreshId === state.refreshId && !controller.signal.aborted && !silent) toast(error.message, "error");
    return false;
  } finally {
    if (refreshId === state.refreshId) {
      state.refreshing = false;
      state.refreshController = null;
      $("#refreshButton").classList.remove("spinning");
      $("#refreshButton").removeAttribute("aria-busy");
    }
  }
}

function renderAll() {
  renderSummary();
  renderControllerStatus();
  renderHashrateHistory();
  renderWorkloadFilterOptions();
  renderWorkloadSettlementNote();
  renderDevices();
  renderUsers();
  renderWallets();
  renderPools();
  populateSavedPoolSelectors();
  renderMinerPackages();
  populateMinerPackageManagedTypeSelect();
  renderMinerPackageLimits();
  renderAgentReleases();
  renderAgentUpdates();
  renderFlightSheets();
  populateManagedMinerTypeSelect("flight");
  populateManagedMinerTypeSelect("flightSecondary");
  if ($("#flightDialog").open) for (const prefix of ["flight", "flightSecondary"]) {
    populateSeineFlightPackageSelect(prefix);
    if (managedLegElements(prefix).type.value === "seine") renderManagedVersionCompatibility(prefix);
  }
  if ($("#deployDialog").open) syncDeploySelection();
  if ($("#flightBatchApplyDialog").open) syncFlightBatchApplySelection();
  $("#deviceBadge").textContent = state.agents.length;
}

function renderSummary() {
  const total = state.summary.total || 0;
  $("#summaryTotal").textContent = total;
  $("#summaryOnline").textContent = state.summary.online || 0;
  $("#summaryNormalMining").textContent = state.summary.normal_mining || 0;
  $("#summaryAlerts").textContent = state.summary.alerts || 0;
  $("#summaryCommands").textContent = state.summary.active_commands || 0;
  const ratio = total ? Math.round((state.summary.online / total) * 100) : 0;
  $("#onlineRatio").textContent = `${state.summary.online || 0} / ${total} · ${ratio}% 可用`;
  renderOverviewAggregateSummary();
}

function controllerUsageCard(label, value, detail) {
  const numeric = Number(value);
  const valid = value != null && Number.isFinite(numeric) && numeric >= 0;
  return `<article class="controller-metric-card"><span>${escapeHtml(label)}</span><strong>${valid ? escapeHtml(metric(numeric)) : "—"}</strong><div class="controller-usage-line"><i style="width:${valid ? clampPercent(numeric) : 0}%"></i></div><small>${escapeHtml(detail)}</small></article>`;
}

function controllerNetworkCard(label, value, direction) {
  const numeric = Number(value);
  const valid = value != null && Number.isFinite(numeric) && numeric >= 0;
  return `<article class="controller-metric-card network"><span>${escapeHtml(label)}</span><strong>${valid ? `${escapeHtml(formatBytes(numeric))}/s` : "采样中"}</strong><small>${escapeHtml(direction)}</small></article>`;
}

function controllerStorageDetail(value, emptyLabel = "暂无数据") {
  if (!value || typeof value !== "object") return emptyLabel;
  const used = Number(value.used_bytes);
  const total = Number(value.total_bytes);
  if (!Number.isFinite(used) || !Number.isFinite(total)) return emptyLabel;
  if (total === 0) return "未配置";
  return `${formatBytes(used)} / ${formatBytes(total)}`;
}

function renderControllerStatus() {
  const controller = state.summary?.controller && typeof state.summary.controller === "object"
    ? state.summary.controller
    : {};
  const version = String(controller.version || "—");
  const versionElement = $("#controllerVersion");
  if (versionElement) versionElement.textContent = `控制端 v${version}`;
  const meta = $("#controllerStatusMeta");
  const collectedAt = Date.parse(controller.collected_at);
  if (meta) meta.textContent = Number.isFinite(collectedAt)
    ? `采样于 ${new Date(collectedAt).toLocaleTimeString("zh-CN", { hour12: false })}`
    : "等待采样";
  const grid = $("#controllerStatusGrid");
  if (!grid) return;
  const memory = controller.memory || {};
  const swap = controller.swap || {};
  const disk = controller.disk || {};
  const network = controller.network || {};
  grid.innerHTML = [
    controllerUsageCard("CPU", controller.cpu_percent, "控制端实时占用"),
    controllerUsageCard("内存", memory.percent, controllerStorageDetail(memory)),
    controllerUsageCard("交换空间", swap.percent, controllerStorageDetail(swap)),
    controllerUsageCard("数据盘", disk.percent, controllerStorageDetail(disk)),
    controllerNetworkCard("实时接收", network.receive_bytes_per_second, "DOWNLOAD"),
    controllerNetworkCard("实时发送", network.send_bytes_per_second, "UPLOAD"),
  ].join("");
}

const HASHRATE_CHART_COLOR_VARIABLES = [
  "--chart-series-1", "--chart-series-2", "--chart-series-3", "--chart-series-4",
  "--chart-series-5", "--chart-series-6", "--chart-series-7", "--chart-series-8",
];

function hashrateChartColors() {
  return HASHRATE_CHART_COLOR_VARIABLES.map((variable, index) => (
    themeColor(variable, ["#c9f24d", "#6da7ff", "#f4b85d", "#53d890", "#d58bff", "#f06c69", "#61d7d0", "#f59ec4"][index])
  ));
}

function hashrateHistorySeriesLabel(series) {
  if (series?.coin && series.coin !== "UNKNOWN") return String(series.coin);
  return ["未知币种", series?.algorithm, series?.agent_id?.slice(0, 8)].filter(Boolean).join(" · ");
}

function hashrateHistoryPoints(series) {
  const samples = (Array.isArray(series?.points) ? series.points : [])
    .map((point) => ({ point, timestamp: Date.parse(point?.timestamp) }))
    .filter((sample) => Number.isFinite(sample.timestamp))
    .sort((left, right) => left.timestamp - right.timestamp);
  let gapBefore = false;
  return samples.reduce((points, { point, timestamp }) => {
    const raw = Object.prototype.hasOwnProperty.call(point, "value") ? point.value : point.hashrate;
    const value = raw === null || raw === undefined || typeof raw === "boolean"
      || typeof raw === "string" && !raw.trim() ? NaN : Number(raw);
    if (!Number.isFinite(value) || value < 0) { gapBefore = true; return points; }
    points.push({ timestamp, value, agentCount: Number(point.agent_count), gapBefore });
    gapBefore = false;
    return points;
  }, []);
}

function hashrateHistorySegments(points, gapMilliseconds) {
  const sorted = [...points].sort((left, right) => left.timestamp - right.timestamp);
  return sorted.reduce((segments, point) => {
    const current = segments[segments.length - 1];
    const previous = current?.[current.length - 1];
    if (!current || point.gapBefore || (previous && point.timestamp - previous.timestamp > gapMilliseconds)) {
      segments.push([point]);
    } else {
      current.push(point);
    }
    return segments;
  }, []);
}

function renderHashrateHistory() {
  const container = $("#hashrateHistoryChart");
  const meta = $("#hashrateHistoryMeta");
  if (!container || !meta) return;
  if (state.hashrateHistoryError) {
    meta.textContent = "暂时无法读取";
    container.innerHTML = '<div class="hashrate-history-state is-error"><strong>算力历史加载失败</strong><span>'
      + escapeHtml(state.hashrateHistoryError)
      + "；可稍后点击右上角刷新重试。</span></div>";
    return;
  }
  const document = state.hashrateHistory && typeof state.hashrateHistory === "object" ? state.hashrateHistory : {};
  const series = (Array.isArray(document.series) ? document.series : [])
    .map((item) => ({ item, points: hashrateHistoryPoints(item) }))
    .filter((entry) => entry.points.length);
  const hours = Number(document.hours) || 24;
  const bucketMinutes = Number(document.bucket_minutes) || 5;
  meta.textContent = hours + " 小时 · 每 " + bucketMinutes + " 分钟 · " + series.length + " 条序列";
  if (!series.length) {
    container.innerHTML = '<div class="hashrate-history-state"><strong>过去 24 小时暂无算力数据</strong><span>设备开始挖矿并上报实时统计后，这里会按币种汇总绘制。</span></div>';
    return;
  }

  renderHashrateSeries(container, document);
}

function renderHashrateSeries(container, history) {
  const series = (Array.isArray(history.series) ? history.series : [])
    .filter((item) => item && typeof item === "object")
    .map((item) => ({ item, points: hashrateHistoryPoints(item) }));
  const allPoints = series.flatMap((entry) => entry.points);
  const hours = Number(history.hours) > 0 ? Number(history.hours) : 24;
  const bucketMinutes = Number(history.bucket_minutes) > 0 ? Number(history.bucket_minutes) : 5;
  const declaredFrom = Date.parse(history.from), declaredTo = Date.parse(history.to);
  const from = Number.isFinite(declaredFrom) ? declaredFrom : Math.min(...allPoints.map((point) => point.timestamp));
  const toCandidate = Number.isFinite(declaredTo) ? declaredTo : Math.max(...allPoints.map((point) => point.timestamp));
  const to = toCandidate > from ? toCandidate : from + hours * 60 * 60 * 1000;
  const width = 1000, height = 150;
  const gapMilliseconds = bucketMinutes * 60 * 1000 * 1.75;
  const timeLabel = (value) => new Date(value).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const colors = hashrateChartColors();
  const cards = series.map((entry, index) => {
    const label = hashrateHistorySeriesLabel(entry.item), unit = entry.item.unit || "H/s";
    const color = colors[index % colors.length];
    if (!entry.points.length) return `<section class="hashrate-series-card"><header><span><i style="background:${color}"></i><strong>${escapeHtml(label)}</strong></span><b>—</b></header><div class="device-hashrate-empty">此币种暂无有效算力采样</div></section>`;
    const actualMaximum = Math.max(...entry.points.map((point) => point.value));
    const maximumValue = actualMaximum || 1;
    const last = entry.points[entry.points.length - 1];
    const lines = hashrateHistorySegments(entry.points, gapMilliseconds).map((segment) => {
      const positions = segment.map((point) => ({
        x: Math.max(0, Math.min(width, ((point.timestamp - from) / (to - from)) * width)),
        y: height - (point.value / maximumValue) * height,
      }));
      if (positions.length === 1) return `<circle class="hashrate-history-point" style="fill:${color}" cx="${positions[0].x.toFixed(2)}" cy="${positions[0].y.toFixed(2)}" r="3"/>`;
      return `<polyline class="hashrate-history-line" style="stroke:${color}" points="${positions.map((point) => point.x.toFixed(2) + "," + point.y.toFixed(2)).join(" ")}"/>`;
    }).join("");
    return `<section class="hashrate-series-card"><header><span><i style="background:${color}"></i><strong>${escapeHtml(label)}</strong></span><b>${escapeHtml(formatHashrateAmount(last.value, unit))}</b></header>
      <div class="hashrate-history-axis-label"><span>算力 (${escapeHtml(unit)}) · 最高 ${escapeHtml(formatHashrateAmount(actualMaximum, unit))}</span><span>时间</span></div>
      <div class="hashrate-history-plot"><div class="hashrate-history-y"><span>${escapeHtml(formatHashrateAmount(actualMaximum, unit))}</span><span>${escapeHtml(formatHashrateAmount(actualMaximum / 2, unit))}</span><span>${escapeHtml(formatHashrateAmount(0, unit))}</span></div>
      <div class="hashrate-history-canvas"><svg class="hashrate-history-svg" data-series-index="${index}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${escapeHtml(label)} 过去 ${hours} 小时算力，左右方向键查看采样" tabindex="0">
      <line x1="0" y1="0" x2="${width}" y2="0"/><line x1="0" y1="${height / 2}" x2="${width}" y2="${height / 2}"/><line x1="0" y1="${height}" x2="${width}" y2="${height}"/>${lines}
      <line class="hashrate-history-hover-line" x1="0" y1="0" x2="0" y2="${height}" visibility="hidden"/><circle class="hashrate-history-hover-point" cx="0" cy="0" r="4" visibility="hidden"/></svg><div class="hashrate-history-tooltip" role="status" hidden></div></div></div>
      <div class="hashrate-history-time"><span>${escapeHtml(timeLabel(from))}</span><span class="hashrate-history-time-mid">${escapeHtml(timeLabel((from + to) / 2))}</span><span>${escapeHtml(timeLabel(to))}</span></div></section>`;
  }).join("");
  container.innerHTML = '<div class="hashrate-series-grid">' + cards + '</div>';
  bindHashrateHistoryHover(container, series, { from, to, width, height, colors });
}

function bindHashrateHistoryHover(container, series, { from, to, width, height, colors }) {
  container.querySelectorAll(".hashrate-history-svg").forEach((svg) => {
    const index = Number(svg.dataset.seriesIndex), entry = series[index];
    const canvas = svg.closest(".hashrate-history-canvas");
    const tooltip = canvas?.querySelector(".hashrate-history-tooltip");
    const line = svg.querySelector(".hashrate-history-hover-line"), dot = svg.querySelector(".hashrate-history-hover-point");
    if (!entry?.points.length || !canvas || !tooltip || !line || !dot) return;
    const maximum = Math.max(...entry.points.map((point) => point.value)) || 1;
    let selected = entry.points.length - 1;
    const hide = () => {
      tooltip.hidden = true;
      line.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
    };
    const show = (point) => {
      const bounds = canvas.getBoundingClientRect();
      if (!bounds.width || !bounds.height) return;
      const x = Math.max(0, Math.min(width, ((point.timestamp - from) / (to - from)) * width));
      const y = height - (point.value / maximum) * height;
      line.setAttribute("x1", x.toFixed(2)); line.setAttribute("x2", x.toFixed(2)); line.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x.toFixed(2)); dot.setAttribute("cy", y.toFixed(2)); dot.setAttribute("style", "fill:" + colors[index % colors.length]); dot.setAttribute("visibility", "visible");
      const time = new Date(point.timestamp).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
      tooltip.innerHTML = `<strong>${escapeHtml(hashrateHistorySeriesLabel(entry.item))}</strong><span>${escapeHtml(time)}</span><b>${escapeHtml(formatHashrateAmount(point.value, entry.item.unit || "H/s"))}</b>`;
      tooltip.hidden = false;
      const tooltipBounds = tooltip.getBoundingClientRect();
      const px = x / width * bounds.width, py = y / height * bounds.height;
      tooltip.style.left = Math.max(6, Math.min(bounds.width - tooltipBounds.width - 6, px - tooltipBounds.width / 2)) + "px";
      tooltip.style.top = Math.max(6, Math.min(bounds.height - tooltipBounds.height - 6, py >= tooltipBounds.height + 12 ? py - tooltipBounds.height - 9 : py + 9)) + "px";
    };
    const pointer = (event) => {
      const bounds = svg.getBoundingClientRect();
      if (!bounds.width) return;
      const target = from + Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)) * (to - from);
      selected = entry.points.reduce((best, point, candidate) => Math.abs(point.timestamp - target) < Math.abs(entry.points[best].timestamp - target) ? candidate : best, 0);
      show(entry.points[selected]);
    };
    svg.addEventListener("pointerenter", pointer);
    svg.addEventListener("pointermove", pointer);
    svg.addEventListener("pointerdown", (event) => { svg.focus({ preventScroll: true }); pointer(event); });
    svg.addEventListener("pointerleave", (event) => { if (event.pointerType !== "touch") hide(); });
    svg.addEventListener("focus", () => show(entry.points[selected]));
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { hide(); return; }
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      selected = event.key === "Home" ? 0 : event.key === "End" ? entry.points.length - 1
        : Math.max(0, Math.min(entry.points.length - 1, selected + (event.key === "ArrowLeft" ? -1 : 1)));
      show(entry.points[selected]);
    });
  });
}

const WORKLOAD_MAX_RANGE_MS = 14 * 24 * 60 * 60 * 1000;

function localDateTimeInputValue(value) {
  const date = value instanceof Date ? value : new Date(value);
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function applyWorkloadPresetRange(hours) {
  const to = new Date(Math.floor(Date.now() / 60_000) * 60_000);
  const from = new Date(to.getTime() - hours * 3_600_000);
  const fromInput = $("#workloadFrom");
  const toInput = $("#workloadTo");
  if (fromInput) fromInput.value = localDateTimeInputValue(from);
  if (toInput) toInput.value = localDateTimeInputValue(to);
  return { from, to };
}

function setWorkloadPreset(hours, shouldFetch = true) {
  const numericHours = Number(hours);
  if (!Number.isFinite(numericHours) || numericHours <= 0 || numericHours > 336) return;
  applyWorkloadPresetRange(numericHours);
  $$('[data-workload-preset]').forEach((button) => button.classList.toggle(
    "active",
    Number(button.dataset.workloadPreset) === numericHours,
  ));
  if (shouldFetch && state.page === "workload") refreshWorkloadSummary();
}

function workloadRangeForRequest() {
  const presetHours = Number($("[data-workload-preset].active")?.dataset.workloadPreset);
  const { from, to } = [24, 168, 336].includes(presetHours)
    ? applyWorkloadPresetRange(presetHours)
    : { from: new Date($("#workloadFrom")?.value || ""), to: new Date($("#workloadTo")?.value || "") };
  if (!Number.isFinite(from.getTime()) || !Number.isFinite(to.getTime())) {
    throw new Error("请选择有效的开始和结束时间");
  }
  if (from >= to) throw new Error("开始时间必须早于结束时间");
  if (to.getTime() - from.getTime() > WORKLOAD_MAX_RANGE_MS) {
    throw new Error("工作量查询时间最长为 14 天");
  }
  const query = new URLSearchParams({
    from: from.toISOString(),
    to: to.toISOString(),
    include_points: "false",
  });
  const agentId = $("#workloadAgent")?.value || "";
  if (agentId) query.set("agent_id", agentId);
  return { from, to, path: `/api/workload?${query.toString()}` };
}

function renderWorkloadFilterOptions() {
  const agentSelect = $("#workloadAgent");
  if (agentSelect) {
    const selected = agentSelect.value;
    const agents = [...state.agents].sort((left, right) => String(left.name || left.hostname || "").localeCompare(
      String(right.name || right.hostname || ""),
      "zh-CN",
    ));
    const signature = JSON.stringify(agents.map((agent) => [agent.id, agent.name || agent.hostname || agent.id]));
    if (agentSelect.dataset.agents !== signature) {
      agentSelect.innerHTML = '<option value="">全部设备</option>' + agents.map((agent) => (
        `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name || agent.hostname || agent.id)}</option>`
      )).join("");
      agentSelect.dataset.agents = signature;
    }
    agentSelect.value = agents.some((agent) => agent.id === selected) ? selected : "";
  }

  renderWorkloadCoinControls();
  renderWorkloadOwnerControls();
}

const WORKLOAD_FOLD_HASHES = 1e9;
const WORKLOAD_COLORS = ["#9bc75b", "#6da7ff", "#f4b85d", "#ce8fe7", "#60cab9", "#ef8581"];

function workloadCanonicalCoin(value) {
  if (typeof value !== "string") return "";
  const coin = value.trim().toUpperCase();
  return /^[A-Z0-9._+-]{1,24}$/.test(coin) ? (coin === "PRL" ? "PEARL" : coin) : "";
}

function workloadWeightNumber(value) {
  if (typeof value !== "string" && typeof value !== "number") return null;
  const raw = String(value).trim();
  if (!raw || raw.length > 64 || !/^-?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw)) return null;
  const numeric = Number(raw);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function loadWorkloadPreferences() {
  const username = state.user?.username || "";
  if (!username || state.workloadPreferencesUser === username) return;
  const preferences = { mode: "multiple", coins: null, weights: {} };
  try {
    const stored = JSON.parse(localStorage.getItem(`rigpilot_workload_preferences_v1:${username}`) || "null");
    if (stored && typeof stored === "object" && !Array.isArray(stored)) {
      if (stored.mode === "single") preferences.mode = "single";
      if (Array.isArray(stored.coins)) {
        preferences.coins = [...new Set(stored.coins.map(workloadCanonicalCoin).filter(Boolean))].slice(0, 64);
      }
      if (stored.weights && typeof stored.weights === "object" && !Array.isArray(stored.weights)) {
        Object.entries(stored.weights).slice(0, 256).forEach(([key, value]) => {
          const coin = workloadCanonicalCoin(key);
          if (coin && workloadWeightNumber(value) !== null) preferences.weights[coin] = String(value);
        });
      }
    }
  } catch { /* A damaged or unavailable preference never implies a default weight. */ }
  if (preferences.mode === "single" && preferences.coins?.length > 1) preferences.coins = preferences.coins.slice(0, 1);
  state.workloadPreferences = preferences;
  state.workloadPreferencesUser = username;
  state.workloadPreferencesStorageError = false;
}

function persistWorkloadPreferences() {
  const username = state.user?.username || "";
  if (!username) return;
  const preferences = state.workloadPreferences;
  const weights = {};
  Object.entries(preferences.weights).forEach(([coin, value]) => {
    if (workloadWeightNumber(value) !== null) weights[coin] = String(value);
  });
  try {
    localStorage.setItem(`rigpilot_workload_preferences_v1:${username}`, JSON.stringify({
      mode: preferences.mode, coins: preferences.coins, weights,
    }));
    state.workloadPreferencesStorageError = false;
  } catch { state.workloadPreferencesStorageError = true; }
}

function supportedWorkloadCoins() {
  const data = state.workload || {};
  const preferences = state.workloadPreferences;
  const candidates = [
    ...(Array.isArray(data.supported_coins) ? data.supported_coins : []),
    ...(Array.isArray(data.available_coins) ? data.available_coins : []),
    ...(Array.isArray(data.series) ? data.series.map((item) => item?.coin) : []),
    ...Object.keys(data.fleet_totals_by_coin || data.fleet_totals || {}),
    ...(preferences.coins || []), ...Object.keys(preferences.weights),
  ];
  return [...new Set(candidates.map(workloadCanonicalCoin).filter(Boolean))].sort();
}

function selectedWorkloadCoins() {
  const preferences = state.workloadPreferences;
  const available = supportedWorkloadCoins();
  const selected = preferences.coins === null ? available : preferences.coins.filter((coin) => available.includes(coin));
  return preferences.mode === "single" ? selected.slice(0, 1) : [...new Set(selected)];
}

function chooseWorkloadCoin(coin) {
  if (!supportedWorkloadCoins().includes(coin)) return;
  const preferences = state.workloadPreferences;
  if (preferences.mode === "single") preferences.coins = [coin];
  else {
    const selected = new Set(selectedWorkloadCoins());
    if (selected.has(coin)) selected.delete(coin); else selected.add(coin);
    preferences.coins = [...selected];
  }
  persistWorkloadPreferences();
  renderWorkloadCoinControls();
  renderWorkloadSummary();
}

function setWorkloadCoinMode(mode) {
  if (!["single", "multiple"].includes(mode)) return;
  const selected = selectedWorkloadCoins();
  state.workloadPreferences.mode = mode;
  state.workloadPreferences.coins = mode === "single" ? selected.slice(0, 1) : selected;
  persistWorkloadPreferences();
  renderWorkloadCoinControls();
  renderWorkloadSummary();
}

function renderWorkloadCoinControls() {
  const root = $("#workloadCoinOptions");
  if (!root) return;
  const coins = supportedWorkloadCoins();
  const selected = new Set(selectedWorkloadCoins());
  const signature = JSON.stringify(coins);
  if (root.dataset.coins !== signature) {
    root.dataset.coins = signature;
    root.innerHTML = coins.map((coin, index) => `<button type="button" class="btn btn-secondary btn-small" data-workload-coin="${escapeHtml(coin)}" style="--coin-color:${WORKLOAD_COLORS[index % WORKLOAD_COLORS.length]}">${escapeHtml(coin)}</button>`).join("") || '<small>所选时间范围暂无已上报币种</small>';
    $$('[data-workload-coin]', root).forEach((button) => button.addEventListener("click", () => chooseWorkloadCoin(button.dataset.workloadCoin)));
  }
  $$('[data-workload-coin]', root).forEach((button) => {
    const active = selected.has(button.dataset.workloadCoin);
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $$('[data-workload-coin-mode]').forEach((button) => {
    const active = button.dataset.workloadCoinMode === state.workloadPreferences.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  const weights = $("#workloadCoinWeights");
  let focusedCoin = "";
  if (weights && weights.dataset.coins !== signature) {
    focusedCoin = document.activeElement?.dataset?.workloadWeight || "";
    weights.dataset.coins = signature;
    weights.innerHTML = coins.map((coin) => `<label data-workload-weight-row="${escapeHtml(coin)}">${escapeHtml(coin)} 权重<input type="number" min="0" step="any" inputmode="decimal" data-workload-weight="${escapeHtml(coin)}" aria-label="${escapeHtml(coin)} 每 GH 的折算单位权重" placeholder="待设置"><small>折算单位 / GH</small></label>`).join("");
    $$('[data-workload-weight]', weights).forEach((input) => input.addEventListener("input", () => {
      state.workloadPreferences.weights[input.dataset.workloadWeight] = input.validity?.badInput ? "" : input.value;
      persistWorkloadPreferences();
      renderWorkloadSummary();
    }));
  }
  $$('[data-workload-weight]', weights || root).forEach((input) => {
    input.disabled = !selected.has(input.dataset.workloadWeight);
    if (document.activeElement !== input) input.value = state.workloadPreferences.weights[input.dataset.workloadWeight] ?? "";
  });
  if (focusedCoin) $$('[data-workload-weight]', weights).find((input) => input.dataset.workloadWeight === focusedCoin)?.focus();
  const legacy = $("#workloadAlgorithm");
  if (legacy) {
    legacy.innerHTML = '<option value=""></option>' + coins.map((coin) => `<option value="${escapeHtml(coin)}">${escapeHtml(coin)}</option>`).join("");
    legacy.value = selected.size === 1 ? [...selected][0] : "";
  }
}

function workloadValueNumber(value) {
  const numeric = Number(value?.value_number ?? value?.value ?? 0);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : 0;
}

function formatWorkAmount(value, unit = "H") {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "—";
  const suffixes = ["H", "kH", "MH", "GH", "TH", "PH", "EH", "ZH", "YH"];
  if (String(unit || "H").toUpperCase() !== "H") return `${numeric.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${unit}`;
  let scaled = numeric;
  let suffixIndex = 0;
  while (scaled >= 1000 && suffixIndex < suffixes.length - 1) {
    scaled /= 1000;
    suffixIndex += 1;
  }
  const maximumFractionDigits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
  return `${scaled.toLocaleString("zh-CN", { maximumFractionDigits })} ${suffixes[suffixIndex]}`;
}

function formatWorkloadPercentage(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return "0%";
  const maximumFractionDigits = numeric >= 10 ? 1 : 2;
  return `${numeric.toLocaleString("zh-CN", { maximumFractionDigits })}%`;
}

function workloadOwnerAgentId(value) {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value) ? value : "";
}

function workloadOwnerRecord(value) {
  const record = { revision: "", groups: [] };
  if (!value || typeof value !== "object" || Array.isArray(value)) return record;
  if (typeof value.revision === "string" && value.revision.length <= 128) record.revision = value.revision;
  const ids = new Set(), assigned = new Set();
  for (const source of Array.isArray(value.groups) ? value.groups : []) {
    const id = workloadOwnerAgentId(source?.id);
    const name = typeof source?.name === "string" ? source.name.trim() : "";
    if (!id || ids.has(id) || !name || name.length > 80 || /[\x00-\x1f\x7f]/.test(name)) continue;
    ids.add(id);
    const agents = [...new Set((Array.isArray(source.agent_ids) ? source.agent_ids : []).map(workloadOwnerAgentId).filter(Boolean))]
      .filter((agent) => { if (assigned.has(agent)) return false; assigned.add(agent); return true; });
    if (agents.length) record.groups.push({ id, name, agent_ids: agents });
  }
  return record;
}

function workloadOwnersKey() {
  return `rigpilot_workload_owners_v1:${state.user?.username || ""}`;
}

function readWorkloadOwners() {
  try {
    const raw = localStorage.getItem(workloadOwnersKey());
    if (raw === null || raw.length > 1024 * 1024) return null;
    const value = JSON.parse(raw);
    if (!value || typeof value.revision !== "string" || !Array.isArray(value.groups)) return null;
    const record = workloadOwnerRecord(value);
    // Invalid legacy records must not be imported as an empty server setting.
    if (record.groups.length !== value.groups.length || value.groups.some((group, index) => (
      group.id !== record.groups[index].id || group.name !== record.groups[index].name
        || JSON.stringify(group.agent_ids) !== JSON.stringify(record.groups[index].agent_ids)
    ))) return null;
    return record.groups;
  } catch { return null; }
}

function workloadServerOwnerRecord(value) {
  if (!value || typeof value.initialized !== "boolean" || !Number.isSafeInteger(value.revision)
      || value.revision < 0 || !Array.isArray(value.groups)
      || (value.initialized ? value.revision < 1 : value.revision !== 0 || value.groups.length !== 0)) {
    throw new Error("服务器归属记录格式异常");
  }
  const groups = workloadOwnerRecord(value).groups;
  if (groups.length !== value.groups.length || value.groups.some((group, index) => (
    group.id !== groups[index].id || group.name !== groups[index].name
      || JSON.stringify(group.agent_ids) !== JSON.stringify(groups[index].agent_ids)
  ))) throw new Error("服务器归属记录格式异常");
  return { initialized: value.initialized, revision: value.revision, groups, updated_at: value.updated_at || null };
}

function workloadOwnersContext() {
  return { authEpoch: state.authEpoch, token: state.token, username: state.user?.username,
    requestId: ++state.workloadOwnersRequestId };
}

function workloadOwnersContextCurrent(context) {
  return Boolean(context.username && context.token && context.authEpoch === state.authEpoch
    && context.token === state.token && context.username === state.user?.username
    && context.requestId === state.workloadOwnersRequestId);
}

async function loadWorkloadOwners({ discardDraft = false, migrate = true } = {}) {
  const username = state.user?.username;
  if (!username || !state.token || state.workloadOwnersBusy) return false;
  if (state.workloadOwnersUser !== username) {
    state.workloadOwners = { initialized: false, revision: 0, groups: [] };
    state.workloadOwnersLoaded = false;
    state.workloadOwnersUser = username;
    state.workloadOwnerDraft = null;
    state.workloadOwnerError = "";
    state.workloadOwnersLegacy = readWorkloadOwners();
  }
  const context = workloadOwnersContext();
  state.workloadOwnersBusy = "loading";
  renderWorkloadOwnerControls();
  try {
    let record = workloadServerOwnerRecord(await api("/api/workload-owner-groups"));
    if (!workloadOwnersContextCurrent(context)) return false;
    // An existing empty row is initialized too. Only an actual missing row
    // can migrate browser data, and CAS 0 protects concurrent first imports.
    if (migrate && !record.initialized && state.workloadOwnersLegacy !== null && !state.workloadOwnerDraft) {
      state.workloadOwnersBusy = "saving";
      renderWorkloadOwnerControls();
      try {
        record = workloadServerOwnerRecord(await api("/api/workload-owner-groups", {
          method: "PUT", body: { expected_revision: 0, groups: state.workloadOwnersLegacy },
        }));
      } catch (error) {
        if (!workloadOwnersContextCurrent(context)) return false;
        if (error.status !== 409) throw error;
        record = workloadServerOwnerRecord(await api("/api/workload-owner-groups"));
      }
      if (!workloadOwnersContextCurrent(context)) return false;
    }
    state.workloadOwners = record;
    state.workloadOwnersLoaded = true;
    if (JSON.stringify(record.groups) === JSON.stringify(state.workloadOwnersLegacy)) state.workloadOwnersLegacy = null;
    if (discardDraft) state.workloadOwnerDraft = null;
    // A refresh does not erase an outstanding save error or editing draft.
    if (!state.workloadOwnerDraft) state.workloadOwnerError = "";
    return true;
  } catch (error) {
    if (!workloadOwnersContextCurrent(context)) return false;
    state.workloadOwnerError = `归属未保存或载入失败：${error.message || "请重试"}。当前草稿已保留。`;
    return false;
  } finally {
    if (workloadOwnersContextCurrent(context)) {
      state.workloadOwnersBusy = "";
      renderWorkloadOwnerControls();
      renderWorkloadSummary();
    }
  }
}

async function commitWorkloadOwners(groups, expectedRevision) {
  if (!state.workloadOwnersLoaded || state.workloadOwnersBusy || !state.user?.username) return false;
  if (expectedRevision !== state.workloadOwners.revision) {
    state.workloadOwnerError = "未保存：服务器归属已更新，当前草稿已保留。请载入服务器归属后再编辑。";
    return false;
  }
  const context = workloadOwnersContext();
  state.workloadOwnersBusy = "saving";
  renderWorkloadOwnerControls();
  try {
    const record = workloadServerOwnerRecord(await api("/api/workload-owner-groups", {
      method: "PUT", body: { expected_revision: expectedRevision, groups },
    }));
    if (!workloadOwnersContextCurrent(context)) return false;
    state.workloadOwners = record;
    state.workloadOwnerError = "";
    return true;
  } catch (error) {
    if (!workloadOwnersContextCurrent(context)) return false;
    state.workloadOwnerError = error.status === 409
      ? "未保存：归属已被其他页面修改，当前草稿已保留。请载入服务器归属后再编辑。"
      : `未保存：${error.message || "服务器保存失败"}。当前草稿已保留。`;
    return false;
  } finally {
    if (workloadOwnersContextCurrent(context)) {
      state.workloadOwnersBusy = "";
      renderWorkloadOwnerControls();
      renderWorkloadSummary();
    }
  }
}

async function importLegacyWorkloadOwners() {
  if (state.workloadOwnersLegacy === null || state.workloadOwnerDraft) return false;
  const legacy = state.workloadOwnersLegacy;
  if (!await commitWorkloadOwners(legacy, state.workloadOwners.revision)) return false;
  if (state.workloadOwnersLegacy === legacy) state.workloadOwnersLegacy = null;
  renderWorkloadOwnerControls();
  return true;
}

function workloadOwnerAgentCatalog() {
  const agents = new Map();
  for (const agent of state.agents) {
    const id = workloadOwnerAgentId(agent?.id);
    if (id) agents.set(id, { id, name: agent.name || agent.hostname || id, hostname: agent.hostname || "", status: agent.status || "" });
  }
  for (const series of state.workload?.series || []) {
    for (const device of workloadIndividualDevices(series)) {
      const id = workloadOwnerAgentId(device?.agent_id);
      if (id && !agents.has(id)) agents.set(id, { id, name: device.agent_name || device.hostname || id, hostname: device.hostname || "", status: "" });
    }
  }
  const known = new Set([...(state.workloadOwners?.groups || []).flatMap((group) => group.agent_ids), ...(state.workloadOwnerDraft?.agent_ids || [])]);
  for (const id of known) if (!agents.has(id)) agents.set(id, { id, name: id, hostname: "", status: "未在当前设备列表" });
  return [...agents.values()].sort((a, b) => a.name.localeCompare(b.name, "zh-CN") || a.id.localeCompare(b.id));
}

function beginWorkloadOwnerEdit(id = "") {
  if (!state.workloadOwnersLoaded || state.workloadOwnersBusy) return;
  const group = state.workloadOwners.groups.find((item) => item.id === id);
  if (id && !group) return;
  state.workloadOwnerDraft = { id, name: group?.name || "", agent_ids: [...(group?.agent_ids || [])], baseRevision: state.workloadOwners.revision };
  state.workloadOwnerError = "";
  renderWorkloadOwnerControls();
  $("#workloadOwnerName")?.focus();
}

function cancelWorkloadOwnerEdit() {
  if (state.workloadOwnersBusy) return;
  state.workloadOwnerDraft = null;
  state.workloadOwnerError = "";
  renderWorkloadOwnerControls();
  renderWorkloadSummary();
}

async function reloadSavedWorkloadOwners() {
  return loadWorkloadOwners({ discardDraft: true, migrate: false });
}

async function saveWorkloadOwner() {
  const draft = state.workloadOwnerDraft;
  if (!draft || !state.workloadOwnersLoaded || state.workloadOwnersBusy) return;
  const name = draft.name.trim();
  const agents = [...new Set(draft.agent_ids.map(workloadOwnerAgentId).filter(Boolean))];
  if (!name || name.length > 80 || /[\x00-\x1f\x7f]/.test(name) || !agents.length) {
    state.workloadOwnerError = "请填写有效人名并至少选择一台矿工。";
    renderWorkloadOwnerControls();
    return;
  }
  if (state.workloadOwners.groups.some((group) => group.id !== draft.id && group.name === name)) {
    state.workloadOwnerError = "已有相同人名，请编辑该分组或使用能区分的人名。";
    renderWorkloadOwnerControls();
    return;
  }
  const id = draft.id || globalThis.crypto?.randomUUID?.() || `owner-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const selected = new Set(agents);
  const groups = state.workloadOwners.groups.filter((group) => group.id !== draft.id)
    .map((group) => ({ ...group, agent_ids: group.agent_ids.filter((agent) => !selected.has(agent)) }))
    .filter((group) => group.agent_ids.length);
  groups.push({ id, name, agent_ids: agents });
  if (await commitWorkloadOwners(groups, draft.baseRevision) && state.workloadOwnerDraft === draft) {
    state.workloadOwnerDraft = null;
    state.workloadOwnerError = "";
  }
  renderWorkloadOwnerControls();
  renderWorkloadSummary();
}

async function removeWorkloadOwner(id) {
  if (!state.workloadOwnersLoaded || state.workloadOwnersBusy || state.workloadOwnerDraft) return;
  const expectedRevision = state.workloadOwners.revision;
  if (!state.workloadOwners.groups.some((group) => group.id === id)) return;
  const groups = state.workloadOwners.groups.filter((group) => group.id !== id);
  if (await commitWorkloadOwners(groups, expectedRevision)) {
    if (state.workloadOwnerDraft?.id === id) state.workloadOwnerDraft = null;
    state.workloadOwnerError = "";
  }
  renderWorkloadOwnerControls();
  renderWorkloadSummary();
}

function renderWorkloadOwnerControls() {
  const list = $("#workloadOwnerGroups");
  if (!list) return;
  const groups = state.workloadOwners.groups;
  const blocked = !state.workloadOwnersLoaded || Boolean(state.workloadOwnersBusy);
  const signature = JSON.stringify(groups);
  if (list.dataset.groups !== signature) {
    list.dataset.groups = signature;
    list.innerHTML = groups.map((group) => `<div class="workload-owner-group"><div><strong>${escapeHtml(group.name)}</strong><small>${group.agent_ids.length} 台矿工</small></div><button type="button" class="btn btn-secondary btn-small" data-edit-workload-owner="${escapeHtml(group.id)}">编辑</button><button type="button" class="btn btn-secondary btn-small" data-remove-workload-owner="${escapeHtml(group.id)}">解除分组</button></div>`).join("") || '<small>未分组矿工按各自 Agent ID 独立显示。</small>';
    $$('[data-edit-workload-owner]', list).forEach((button) => button.addEventListener("click", () => beginWorkloadOwnerEdit(button.dataset.editWorkloadOwner)));
    $$('[data-remove-workload-owner]', list).forEach((button) => button.addEventListener("click", () => removeWorkloadOwner(button.dataset.removeWorkloadOwner)));
  }
  const draft = state.workloadOwnerDraft;
  const add = $("#workloadOwnerAdd"), reload = $("#workloadOwnerReload"), legacy = $("#workloadOwnerImport");
  if (add) add.disabled = blocked;
  if (reload) reload.disabled = Boolean(state.workloadOwnersBusy);
  if (legacy) { legacy.hidden = state.workloadOwnersLegacy === null; legacy.disabled = blocked || Boolean(draft); }
  $$('[data-edit-workload-owner], [data-remove-workload-owner]', list).forEach((button) => { button.disabled = blocked || Boolean(draft); });
  const form = $("#workloadOwnerForm");
  if (form) {
    form.hidden = !draft;
    $$('button', form).forEach((button) => { button.disabled = blocked; });
    // Background reads leave focused draft inputs usable and intact.
    $$('input', form).forEach((input) => { input.disabled = !state.workloadOwnersLoaded || state.workloadOwnersBusy === "saving"; });
  }
  const name = $("#workloadOwnerName");
  if (name && draft && document.activeElement !== name) name.value = draft.name;
  const picker = $("#workloadOwnerAgents");
  if (picker && draft) {
    const catalog = workloadOwnerAgentCatalog();
    const choices = JSON.stringify([catalog, signature]);
    const assignments = workloadOwnerAssignments();
    if (picker.dataset.choices !== choices) {
      const focusedId = document.activeElement?.dataset?.workloadOwnerAgent;
      picker.dataset.choices = choices;
      picker.innerHTML = catalog.map((agent) => `<label><input type="checkbox" data-workload-owner-agent="${escapeHtml(agent.id)}"><span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml([agent.hostname, agent.id, agent.status === "offline" ? "离线" : agent.status, assignments.get(agent.id)?.name ? `当前归属：${assignments.get(agent.id).name}` : "未分组"].filter(Boolean).join(" · "))}</small></span></label>`).join("");
      $$('[data-workload-owner-agent]', picker).forEach((input) => {
        input.checked = draft.agent_ids.includes(input.dataset.workloadOwnerAgent);
        input.disabled = !state.workloadOwnersLoaded || state.workloadOwnersBusy === "saving";
        input.addEventListener("change", () => {
          const current = state.workloadOwnerDraft;
          if (!current || state.workloadOwnersBusy === "saving") return;
          const selected = new Set(current.agent_ids);
          if (input.checked) selected.add(input.dataset.workloadOwnerAgent); else selected.delete(input.dataset.workloadOwnerAgent);
          current.agent_ids = [...selected];
        });
      });
      if (focusedId) $$('[data-workload-owner-agent]', picker).find((input) => input.dataset.workloadOwnerAgent === focusedId)?.focus();
    } else $$('[data-workload-owner-agent]', picker).forEach((input) => {
      input.checked = draft.agent_ids.includes(input.dataset.workloadOwnerAgent);
      input.disabled = !state.workloadOwnersLoaded || state.workloadOwnersBusy === "saving";
    });
  }
  const error = $("#workloadOwnerError");
  if (error) error.textContent = state.workloadOwnerError;
  const notice = $("#workloadOwnerNotice");
  if (notice) notice.textContent = state.workloadOwnerError;
  const status = $("#workloadOwnersStatus");
  if (status) status.textContent = state.workloadOwnersBusy === "saving" ? "正在保存到服务器…"
    : state.workloadOwnersBusy ? "正在载入服务器归属…"
      : !state.workloadOwnersLoaded ? "尚未载入服务器归属，请重试。"
        : draft ? "草稿未保存；归属按登录账号保存在服务器。"
          : "归属按登录账号保存在服务器，可在不同浏览器使用；原始历史不改。";
}

function workloadIndividualDevices(series) {
  if (Array.isArray(series?.individual_devices)) return series.individual_devices;
  // An old name-merged amount cannot be assigned to its representative ID.
  return (Array.isArray(series?.devices) ? series.devices : []).filter((device) => (
    device?.agent_id && !(Number(device.device_count) > 1)
      && (!Array.isArray(device.agent_ids) || device.agent_ids.length <= 1)
  ));
}

function workloadOwnerAssignments() {
  const assignments = new Map();
  for (const group of state.workloadOwners?.groups || []) {
    for (const id of group.agent_ids) assignments.set(id, group);
  }
  return assignments;
}

function workloadCombinedModel() {
  const data = state.workload || {};
  const selected = selectedWorkloadCoins();
  const selectedSet = new Set(selected);
  const weights = state.workloadPreferences.weights;
  const missing = selected.filter((coin) => workloadWeightNumber(weights[coin]) === null);
  const series = new Map();
  (Array.isArray(data.series) ? data.series : []).forEach((item) => {
    const coin = workloadCanonicalCoin(item?.coin);
    if (coin && selectedSet.has(coin) && !series.has(coin) && Array.isArray(item.devices)) series.set(coin, item);
  });
  let total = 0;
  let overflow = false;
  let detailUnavailable = false;
  const devices = new Map();
  for (const coin of selected) {
    const item = series.get(coin);
    const suppliedTotal = data.fleet_totals_by_coin?.[coin] ?? data.fleet_totals?.[coin] ?? item?.series_total ?? item?.total_number ?? item?.total ?? 0;
    const rawTotal = Number(suppliedTotal);
    const weight = workloadWeightNumber(weights[coin]);
    const folded = weight === null ? null : rawTotal / WORKLOAD_FOLD_HASHES * weight;
    if (!Number.isFinite(rawTotal) || rawTotal < 0 || (folded !== null && !Number.isFinite(folded))) overflow = true;
    else if (folded !== null) total += folded;
    if (item && !Array.isArray(item.individual_devices)
        && item.devices.some((device) => Number(device?.device_count) > 1 || device?.agent_ids?.length > 1)) detailUnavailable = true;
    const seen = new Set();
    for (const device of workloadIndividualDevices(item)) {
      const id = workloadOwnerAgentId(device?.agent_id);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      let row = devices.get(id);
      if (!row) {
        row = { id, name: device.agent_name || device.hostname || id, hostname: device.hostname || "", agent_ids: new Set([id]), raw: {}, value: 0, has_coverage_gap: false };
        devices.set(id, row);
      }
      const amount = workloadValueNumber(device);
      row.raw[coin] = amount;
      row.has_coverage_gap ||= device.has_coverage_gap === true;
      if (weight !== null) row.value += amount / WORKLOAD_FOLD_HASHES * weight;
      if (!Number.isFinite(row.value)) overflow = true;
    }
  }
  const assignments = workloadOwnerAssignments();
  const groups = new Map();
  for (const device of devices.values()) {
    const owner = assignments.get(device.id);
    const key = owner ? `owner:${owner.id}` : `agent:${device.id}`;
    let group = groups.get(key);
    if (!group) {
      group = { id: key, name: owner?.name || device.name, owner_id: owner?.id || "", agent_ids: new Set(), members: [], raw: {}, value: 0, has_coverage_gap: false };
      groups.set(key, group);
    }
    group.members.push(device);
    group.agent_ids.add(device.id);
    group.value += device.value;
    for (const [coin, amount] of Object.entries(device.raw)) group.raw[coin] = (group.raw[coin] || 0) + amount;
    group.has_coverage_gap ||= device.has_coverage_gap;
    if (!Number.isFinite(group.value)) overflow = true;
  }
  if (!Number.isFinite(total)) overflow = true;
  const ready = selected.length > 0 && missing.length === 0 && !overflow && !detailUnavailable;
  const rows = [...groups.values()].sort((left, right) => (ready ? right.value - left.value : 0) || left.name.localeCompare(right.name, "zh-CN") || left.id.localeCompare(right.id));
  return { coins: selected, missing, ready, overflow, detailUnavailable, total: ready ? total : null, rows };
}

function formatWorkloadWeightedAmount(value) {
  if (!Number.isFinite(value) || value < 0) return "—";
  return `${value > 0 && value < 1e-6 ? value.toExponential(3) : value.toLocaleString("zh-CN", { maximumSignificantDigits: 6 })} WU`;
}

function syncWorkloadRevenueInput(model) {
  const input = $("#workloadRevenueTotal");
  if (!input) return;
  input.disabled = !model?.ready || !(model.total > 0);
  if (document.activeElement !== input) input.value = state.workloadRevenueAmount;
  const hint = $("#workloadRevenueHint");
  if (hint) hint.textContent = input.disabled ? "设置所选币种权重且折算总量大于 0 后，可按占比分配" : "按当前选中币种的加权全矿场占比分配";
  const status = $("#workloadWeightsStatus");
  if (status) status.textContent = state.workloadPreferencesStorageError
    ? "浏览器未允许保存，当前会话仍保留设置" : "币种选择与有效权重已按账号保存在当前浏览器";
}

function rememberWorkloadRevenueTotal() {
  state.workloadRevenueAmount = $("#workloadRevenueTotal")?.value ?? "";
  renderWorkloadSummary();
}

function workloadRevenueTotal() {
  const input = $("#workloadRevenueTotal");
  if (input?.disabled || input?.validity?.badInput) return null;
  const raw = String(input?.value ?? "").trim();
  if (!raw) return null;
  const numeric = Number(raw);
  return Number.isFinite(numeric) && numeric >= 0 ? numeric : null;
}

function workloadRevenueForPercentage(total, percentage) {
  if (total === null || total === undefined) return null;
  const numericTotal = Number(total);
  const numericPercentage = Number(percentage);
  if (!Number.isFinite(numericTotal) || numericTotal < 0 || !Number.isFinite(numericPercentage)) return null;
  return numericTotal * (Math.max(0, Math.min(100, numericPercentage)) / 100);
}

function formatWorkloadRevenue(value) {
  if (value === null || value === undefined) return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric < 0) return "—";
  const maximumFractionDigits = numeric > 0 && numeric < 0.01 ? 8 : 2;
  return numeric.toLocaleString("zh-CN", {
    minimumFractionDigits: Math.min(2, maximumFractionDigits),
    maximumFractionDigits,
  });
}

function renderWorkloadSummary() {
  const container = $("#workloadSummary");
  if (!container) return;
  if (state.workloadLoading || state.workloadError || !state.workloadHasRequested) {
    syncWorkloadRevenueInput(null);
    container.innerHTML = state.workloadError
      ? `<div class="workload-state is-error"><strong>工作量读取失败</strong><span>${escapeHtml(state.workloadError)}</span></div>`
      : `<div class="workload-state">${state.workloadLoading ? "正在读取工作量…" : "进入工作量页面后读取统计"}</div>`;
    return;
  }
  rememberDisclosures(container);
  const model = workloadCombinedModel();
  syncWorkloadRevenueInput(model);
  if (!model.coins.length || !model.rows.length) {
    container.innerHTML = `<div class="workload-state"><strong>${model.detailUnavailable ? "缺少逐矿工明细，请重新查询" : model.coins.length ? "所选范围暂无工作量" : "请选择一个或多个币种"}</strong><span>仅统计真实已上报的原始积分，不补算未观测时段。</span></div>`;
    return;
  }
  const reason = model.detailUnavailable ? "缺少逐矿工明细，请重新查询后再分组汇总。" : model.overflow ? "权重导致折算值超出范围，请调整" : model.missing.length ? `待设置或修正权重：${model.missing.join("、")}` : model.total === 0 ? "折算总量为 0，暂无可分配收益" : "";
  const revenueTotal = workloadRevenueTotal();
  const rows = model.rows.map((row) => {
    const percentage = model.ready && model.total > 0 ? row.value / model.total * 100 : null;
    const rawDescription = model.coins.filter((coin) => coin in row.raw).map((coin) => `${coin} 原始 ${formatWorkAmount(row.raw[coin], "H")} · 权重 ${state.workloadPreferences.weights[coin] || "未设置"} WU/GH`).join("\n");
    const composition = model.ready && model.total > 0 ? model.coins.map((coin, index) => {
      const contribution = (row.raw[coin] || 0) / WORKLOAD_FOLD_HASHES * workloadWeightNumber(state.workloadPreferences.weights[coin]);
      return `<i style="width:${(contribution / model.total * 100).toFixed(6)}%;background:${WORKLOAD_COLORS[index % WORKLOAD_COLORS.length]}" title="${escapeHtml(`${coin}：${formatWorkAmount(row.raw[coin] || 0, "H")} → ${formatWorkloadWeightedAmount(contribution)}`)}"></i>`;
    }).join("") : "";
    const revenue = percentage === null ? null : workloadRevenueForPercentage(revenueTotal, percentage);
    return `<div class="workload-device-row" title="${escapeHtml(rawDescription)}"><div class="workload-device-name"><strong>${escapeHtml(row.name)}</strong>${row.owner_id ? `<small>归属分组 · ${row.agent_ids.size} 台矿工</small>` : `<small>未分组 · ${escapeHtml(row.members[0]?.hostname || row.members[0]?.id || "")}</small>`}${row.has_coverage_gap ? '<small>含观测中断恢复积分</small>' : ""}</div><div class="workload-measure"><div class="workload-bar workload-stacked-bar">${composition}</div><span class="workload-percentage">${percentage === null ? "—" : escapeHtml(formatWorkloadPercentage(percentage))}</span><b class="workload-value">${model.ready ? escapeHtml(formatWorkloadWeightedAmount(row.value)) : "待折算"}</b><span class="workload-revenue" title="总金额 × 所选币种加权全矿场占比"><small>收益</small><strong>${escapeHtml(formatWorkloadRevenue(revenue))}</strong></span></div><div class="workload-raw-breakdown">${model.coins.filter((coin) => coin in row.raw).map((coin) => `<span>${escapeHtml(coin)} ${escapeHtml(formatWorkAmount(row.raw[coin], "H"))}</span>`).join("")}</div>${row.owner_id ? `<details class="workload-owner-members" data-disclosure-key="${escapeHtml(disclosureKey("workload-owner", row.owner_id))}"><summary>成员明细（${row.members.length} 台）</summary>${row.members.map((member) => `<div><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml([member.hostname, member.id].filter(Boolean).join(" · "))}</small><span>${model.ready ? escapeHtml(formatWorkloadWeightedAmount(member.value)) : "待折算"} · ${escapeHtml(Object.entries(member.raw).map(([coin, value]) => `${coin} ${formatWorkAmount(value, "H")}`).join(" / "))}</span></div>`).join("")}</details>` : ""}</div>`;
  }).join("");
  const legend = model.coins.map((coin, index) => `<span><i style="background:${WORKLOAD_COLORS[index % WORKLOAD_COLORS.length]}"></i>${escapeHtml(coin)} · ${escapeHtml(String(state.workloadPreferences.weights[coin] ?? "未设置"))} WU/GH</span>`).join("");
  container.innerHTML = `<section class="workload-algorithm-card workload-combined-card"><header><div><strong>合并工作量</strong><span class="workload-basis is-estimated">用户权重折算 · 原始 H 为算力积分</span></div><span>所选全矿场合计 <b>${model.ready ? escapeHtml(formatWorkloadWeightedAmount(model.total)) : model.overflow ? "超出范围" : "待设置权重"}</b></span></header>${reason ? `<p class="workload-fold-notice">${escapeHtml(reason)}</p>` : ""}<div class="workload-coin-legend">${legend}</div><div class="workload-device-list">${rows}</div></section>`;
  restoreDisclosures(container);
}

async function refreshWorkloadSummary() {
  const error = $("#workloadError");
  if (error) error.textContent = "";
  let request;
  try {
    request = workloadRangeForRequest();
  } catch (rangeError) {
    state.workloadError = rangeError.message;
    if (error) error.textContent = rangeError.message;
    renderWorkloadSummary();
    return false;
  }
  state.workloadHasRequested = true;
  state.workloadLastRequestAt = Date.now();
  state.workloadLoading = true;
  state.workloadError = "";
  const requestId = ++state.workloadRequestId;
  const authEpoch = state.authEpoch;
  renderWorkloadSummary();
  try {
    const workloadDocument = await api(request.path);
    if (requestId !== state.workloadRequestId || authEpoch !== state.authEpoch) return false;
    state.workload = workloadDocument;
    state.workloadLoading = false;
    renderWorkloadFilterOptions();
    renderWorkloadSummary();
    loadWorkloadOwners({ migrate: false });
    return true;
  } catch (requestError) {
    if (requestId !== state.workloadRequestId || authEpoch !== state.authEpoch) return false;
    state.workloadLoading = false;
    state.workloadError = requestError.message || "工作量读取失败";
    if (error) error.textContent = state.workloadError;
    renderWorkloadSummary();
    return false;
  }
}

function workloadRefreshDue() {
  return state.page === "workload" && Boolean(state.token) && !state.workloadLoading
    && (!state.workloadHasRequested || Date.now() - state.workloadLastRequestAt >= 60000);
}

function settlementLocalValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function settlementIsoFromLocal(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(String(value || ""));
  if (!match || Number(match[5]) % 5 !== 0) throw new Error("结算时间必须选择 5 分钟整点");
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error("请选择有效的结算时间");
  return date.toISOString();
}

function renderWorkloadSettlementNote() {
  const input = $("#workloadSettlementNote");
  const button = $("#workloadSettlementNoteSave");
  const syncButton = $("#workloadSettlementUseAsStart");
  const status = $("#workloadSettlementNoteStatus");
  const legacy = $("#workloadSettlementLegacyNote");
  if (!input || !button || !syncButton || !status || !legacy) return;

  const canSave = state.user?.role === "admin" || state.user?.role === "operator";
  const record = state.settlementNote && typeof state.settlementNote === "object"
    ? state.settlementNote
    : { settlement_at: null, legacy_note: null, updated_at: null, updated_by: null, revision: 0 };
  if (!state.settlementNoteDirty && document.activeElement !== input) input.value = settlementLocalValue(record.settlement_at);
  const ready = state.settlementNoteLoaded;
  input.readOnly = !canSave || !ready || state.settlementNoteSaving;
  button.disabled = !canSave || !ready || state.settlementNoteSaving || !input.value;
  syncButton.disabled = !ready || !record.settlement_at;
  button.textContent = state.settlementNoteSaving ? "保存中…" : "保存结算时间";
  legacy.textContent = record.legacy_note ? `旧版结算备注（只读）：${record.legacy_note}` : "";
  legacy.classList.toggle("hidden", !record.legacy_note);
  status.classList.remove("is-error", "is-success", "is-dirty");

  if (state.settlementNoteSaving) {
    status.textContent = "正在保存到控制端…";
  } else if (state.settlementNoteError) {
    status.textContent = state.settlementNoteError;
    status.classList.add("is-error");
  } else if (!state.settlementNoteLoaded) {
    status.textContent = "正在读取结算时间…";
  } else if (state.settlementNoteNotice) {
    status.textContent = state.settlementNoteNotice;
    status.classList.add("is-dirty");
  } else if (state.settlementNoteDirty) {
    status.textContent = "时间有修改，尚未保存";
    status.classList.add("is-dirty");
  } else if (record.settlement_at) {
    const updatedAt = record.updated_at ? formatTime(record.updated_at, false) : "";
    const updatedBy = String(record.updated_by || "").trim();
    const savedBy = [updatedBy ? `${updatedBy} 保存` : "", updatedAt].filter(Boolean).join(" · ");
    status.textContent = `已保存结算时间${savedBy ? ` · ${savedBy}` : ""}${canSave ? " · 再次保存可刷新记录时间" : " · 只读"}`;
    status.classList.add("is-success");
  } else {
    status.textContent = canSave ? "尚未保存结算时间" : "尚无结算时间 · 只读";
  }
}

function useSavedSettlementAsWorkloadStart() {
  const saved = state.settlementNote?.settlement_at;
  const from = $("#workloadFrom");
  if (!saved || !from) return false;
  from.value = settlementLocalValue(saved);
  $$('[data-workload-preset]').forEach((button) => button.classList.remove("active"));
  toast("工作量开始时间已设为保存的结算时间");
  return true;
}

async function saveWorkloadSettlementNote() {
  const input = $("#workloadSettlementNote");
  if (!input || !state.settlementNoteLoaded || state.settlementNoteSaving) return false;
  if (state.user?.role !== "admin" && state.user?.role !== "operator") return false;
  let settlementAt;
  try {
    settlementAt = settlementIsoFromLocal(input.value);
  } catch (validationError) {
    state.settlementNoteError = validationError.message;
    renderWorkloadSettlementNote();
    return false;
  }

  const authEpoch = state.authEpoch, tokenAtStart = state.token;
  const isCurrent = () => authEpoch === state.authEpoch && tokenAtStart === state.token;
  const draft = input.value;
  state.settlementNoteSaving = true;
  state.settlementNoteError = "";
  renderWorkloadSettlementNote();
  try {
    const record = await api(WORKLOAD_SETTLEMENT_NOTE_PATH, {
      method: "PUT",
      body: {
        settlement_at: settlementAt,
        expected_revision: Number(state.settlementNote?.revision) || 0,
      },
    });
    if (!isCurrent()) return false;
    state.settlementNote = record;
    state.settlementNoteLoaded = true;
    state.settlementNoteDirty = false;
    state.settlementNoteNotice = "";
    input.value = settlementLocalValue(record?.settlement_at);
    toast("结算时间已保存");
    return true;
  } catch (requestError) {
    if (!isCurrent()) return false;
    if (requestError?.status === 409) {
      try {
        const latest = await api(WORKLOAD_SETTLEMENT_NOTE_PATH);
        if (!isCurrent()) return false;
        state.settlementNote = latest;
        state.settlementNoteLoaded = true;
        state.settlementNoteDirty = true;
        state.settlementNoteError = "";
        state.settlementNoteNotice = "服务器结算时间刚被其他用户更新；当前选择已保留，请核对后再次保存";
        input.value = draft;
      } catch (reloadError) {
        if (!isCurrent()) return false;
        state.settlementNoteLoaded = false;
        state.settlementNoteError = reloadError.message || "结算时间版本冲突，读取服务器最新版失败";
      }
    } else {
      state.settlementNoteError = requestError.message || "结算时间保存失败";
    }
    return false;
  } finally {
    if (isCurrent()) {
      state.settlementNoteSaving = false;
      renderWorkloadSettlementNote();
    }
  }
}
function averageGpuUtilization(gpus) {
  const values = (Array.isArray(gpus) ? gpus : [])
    .filter((gpu) => gpu?.driver_available !== false)
    .map((gpu) => gpu?.utilization_percent)
    .filter((value) => value !== null && value !== undefined && value !== "" && typeof value !== "boolean")
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value >= 0);
  return values.length ? values.reduce((total, value) => total + value, 0) / values.length : null;
}

function gpuUtilizationMetric(gpus) {
  const utilization = averageGpuUtilization(gpus);
  return utilization == null ? "—" : metric(utilization);
}

function bindAgentRows(elements) {
  elements.forEach((row) => {
    const activate = (event) => {
      if (event?.target?.closest("button, a, input, select, textarea")) return;
      openAgent(row.dataset.agentId);
    };
    row.tabIndex = 0;
    row.setAttribute("aria-label", `打开设备 ${row.querySelector("strong")?.textContent || "详情"}`);
    row.addEventListener("click", activate);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        if (event.target?.closest("button, a, input, select, textarea")) return;
        event.preventDefault();
        activate(event);
      }
    });
  });
}
function filteredAgents() {
  const query = $("#deviceSearch").value.trim().toLowerCase();
  return state.agents.filter((agent) => {
    const searchable = [agent.name, agent.note, agent.hostname, agent.os_family, agent.platform, agent.public_ip, agent.local_ip, ...(agent.local_ip_addresses || [])].join(" ").toLowerCase();
    const queryMatch = !query || searchable.includes(query);
    const filterMatch = state.filter === "all" || agent.status === state.filter || (state.filter === "alerts" && agent.alerts?.length);
    return queryMatch && filterMatch;
  });
}

function miningStatusForAgent(agent) {
  const inventory = agent?.inventory && typeof agent.inventory === "object" ? agent.inventory : {};
  const metrics = agent?.metrics && typeof agent.metrics === "object" ? agent.metrics : {};
  const status = inventory.mining_status || metrics.mining_status || agent?.mining_status;
  return status && typeof status === "object" ? status : null;
}

function knownPackageBytes(value) {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function renderMinerPackageLimits() {
  const output = $("#minerPackageLimitHint");
  if (!output) return;
  const limits = state.minerPackageLimits;
  const category = limits?.native;
  const archive = knownPackageBytes(category?.max_archive_bytes);
  const free = knownPackageBytes(limits?.storage?.free_bytes);
  output.textContent = archive === null ? "尚未获取软件包大小上限；控制端会在上传或导入时检查。"
    : `压缩包上限 ${formatBytes(archive)}${free === null ? "" : `；控制端当前可用空间 ${formatBytes(free)}`}。归档预算不包含软件包运行后自行下载的数据。`;
}

function validateMinerPackageUploadSize(file) {
  const category = state.minerPackageLimits?.native;
  const limit = knownPackageBytes(category?.max_archive_bytes);
  const size = knownPackageBytes(file?.size);
  if (limit !== null && size !== null && size > limit) throw new Error(`压缩包大小 ${formatBytes(size)} 超过当前类别上限 ${formatBytes(limit)}。`);
}

function deviceRuntimePreparationReason(agent) {
  // HiveOS runtime preparation was removed in Controller 0.8.48. Historical
  // gates stay as no-ops so existing call sites keep their structure.
  void agent;
  return "";
}

function pendingMiningControlReason(agent) {
  return (Array.isArray(state.commands) ? state.commands : []).some((command) => command.agent_id === agent?.id
    && ["stop_mining", "restart_mining"].includes(command.action) && ["queued", "dispatched", "running"].includes(command.status))
    ? "已有停止或启动挖矿任务，请等待完成" : "";
}

function miningStatusUsesHiveos(status) {
  return [status, status?.primary, status?.secondary].some((item) => item?.miner_type === "hiveos");
}

function miningProcessIsConfirmed(status, agent) {
  const raw = String(status?.state || status?.service_state || "").toLowerCase();
  if (agent?.status !== "online" || !["running", "active"].includes(raw)
      || status?.process_verified !== true || status?.process_error) return false;
  const checkedAt = Date.parse(status.checked_at);
  const heartbeatAt = Date.parse(agent.last_seen);
  const bootAt = Date.parse(agent.metrics?.boot_time);
  return Number.isFinite(checkedAt) && Number.isFinite(heartbeatAt)
    && Math.abs(Date.now() - heartbeatAt) <= 120000
    && Math.abs(heartbeatAt - checkedAt) <= 60000
    && checkedAt <= Date.now() + 30000
    && (!Number.isFinite(bootAt) || checkedAt >= bootAt);
}

function miningStatisticsLabel(status, agent) {
  const display = miningDisplayState(status, agent);
  if (["running", "active", "degraded"].includes(display)) return "已收到新鲜统计";
  if (display === "running_zero_hashrate") return "已收到新鲜统计（上报算力为 0）";
  if (display === "running_partial_hashrate") return "部分矿工算力待确认";
  return {
    warming_up: "等待首次有效统计", stale: "统计已过期", error: "统计采集失败",
    unavailable: "统计暂不可用", fresh: "统计时间待确认",
  }[String(status?.stats_state || "")] || "等待新鲜统计";
}

function miningStatusErrorMessages(status) {
  if (!status || typeof status !== "object") return [];
  if (!miningStatusUsesHiveos(status)) return [
    status.error,
    status.process_error,
  ].filter(Boolean).map(String);
  const messages = [];
  if (status.process_error) messages.push("矿工进程检查未通过，请采集诊断信息");
  if (status.error && !status.stats_error) messages.push("矿工状态需要检查，请采集诊断信息");
  if (status.stats_error && status.stats_error !== "stats_warming_up") messages.push(`统计采集异常：${status.stats_error}`);
  return messages;
}

function deviceMiningIdentityRows(status) {
  const details = miningHashrateDetails(status);
  const identities = details.length ? details : [
    { role: "primary" }, ...(status.secondary ? [{ role: "secondary" }] : []),
  ];
  return identities.map((detail, index) => deviceMiningTableEntry(status, miningHashrateRole(detail, index),
    null, status.hashrate_unit, { ...detail, hashrate: null, gpu_hashrates: [] }, null));
}

function deviceDiagnosticUnavailableReason(agent) {
  if (!["admin", "operator"].includes(state.user?.role)) return "需要运维人员或管理员权限";
  if (!agent || agent.status !== "online") return "设备离线，无法采集诊断信息";
  return "";
}

function minerLogUnavailableReason(agent) {
  const unavailable = deviceDiagnosticUnavailableReason(agent);
  if (unavailable) return unavailable.replace("诊断信息", "矿工日志");
  if (!agentSupportsMinerLogs(agent.agent_version)) return "需要 RigPilot Agent 0.5.1 或更高版本";
  return "";
}

function minerLogErrorMarkup(result) {
  const logs = Array.isArray(result?.logs) ? result.logs.slice(0, 8) : [];
  const errors = logs.filter((entry) => entry?.error).map((entry) => `${String(entry.source || "日志").split(/[\\/]/).pop().slice(0, 128)}：${entry.error}`);
  if (result?.error) errors.unshift(`日志采集：${result.error}`);
  return errors.length ? `<div class="miner-log-message error" role="status">${errors.map(escapeHtml).join("；")}</div>` : "";
}

function deviceAuditMatchesAgent(entry, agentId, commands) {
  if (entry?.target_type === "agent") return entry.target_id === agentId;
  if (entry?.target_type === "command") {
    const command = commands.find((item) => item.id === entry.target_id);
    if (command) return command.agent_id === agentId;
  }
  return entry?.related_agent_id === agentId
    && ["command", "remote_session", "flight_sheet", "gpu_profile"].includes(entry.target_type);
}

function deviceActivityMarkup(agent) {
  const view = state.deviceLog?.agentId === agent.id ? state.deviceLog : null;
  if (!view) return '<div class="device-activity-empty">打开运行日志后读取该设备的任务与审计记录。</div>';
  const commands = Array.isArray(view.commands) ? view.commands : [];
  const audit = Array.isArray(view.audit) ? view.audit : [];
  const canOperate = ["admin", "operator"].includes(state.user?.role);
  const entries = [
    ...commands.map((command) => {
      const name = actionNames[command.action] || command.action;
      const canCancel = canOperate && command.action !== "agent_update.v1" && command.status === "queued";
      const runtimeCancellable = canOperate && command.action !== "agent_update.v1"
        && RUNTIME_CANCELLABLE_ACTIONS.has(command.action)
        && ["dispatched", "running"].includes(command.status);
      const canAbandon = state.user?.role === "admin" && command.action !== "agent_update.v1"
        && agent.status === "offline" && ["dispatched", "running"].includes(command.status) && !runtimeCancellable;
      const operation = canCancel || runtimeCancellable ? "cancel" : canAbandon ? "abandon" : "";
      const control = operation
        ? `<button class="text-button danger-text" type="button" data-device-command-operation="${operation}" data-command-id="${escapeHtml(command.id)}" data-command-name="${escapeHtml(name)}" data-command-state="${escapeHtml(command.status)}">${operation === "cancel" ? "取消" : "结束遗留"}</button>`
        : "";
      const error = command.error ? ` · ${escapeHtml(commandErrorMessage(command))}` : "";
      return { at: Date.parse(command.created_at) || 0, html: `<div class="device-activity-row"><span class="device-activity-kind">任务</span><div><strong>${escapeHtml(name)}</strong><small>${escapeHtml(formatTime(command.created_at, false))}${error}</small></div><div class="device-activity-result"><span class="command-status ${escapeHtml(command.status)}">${escapeHtml(commandStatus(command.status))}</span>${control}</div></div>` };
    }),
    ...audit.map((entry) => ({ at: Date.parse(entry.created_at) || 0,
      html: `<div class="device-activity-row"><span class="device-activity-kind audit">审计</span><div><strong>${escapeHtml(auditNames[entry.action] || entry.action)}</strong><small>${escapeHtml(entry.actor)} · ${escapeHtml(formatTime(entry.created_at, false))}</small></div></div>` })),
  ].sort((left, right) => right.at - left.at).slice(0, 60);
  const issue = view.error ? `<div class="miner-log-message warning">${escapeHtml(view.error)}</div>` : "";
  if (!entries.length) return issue + (view.loading
    ? '<div class="device-activity-empty">正在读取该设备记录…</div>'
    : '<div class="device-activity-empty">当前没有该设备的任务或审计记录。</div>');
  return issue + `<div class="device-activity-list">${entries.map((entry) => entry.html).join("")}</div>`;
}

function renderDeviceActivity() {
  const agent = state.selectedAgent;
  const container = $("#deviceOperationLog");
  if (agent && container) container.innerHTML = deviceActivityMarkup(agent);
}

async function loadDeviceActivity(agentId, { force = false } = {}) {
  if (!agentId || state.selectedAgent?.id !== agentId) return;
  const previous = state.deviceLog?.agentId === agentId ? state.deviceLog : null;
  if (previous && previous.loading) return;
  if (!force && previous && Date.now() - (previous.fetchedAt || 0) < 15000) return;
  const requestId = ++state.deviceLogRequestId;
  const authEpoch = state.authEpoch;
  state.deviceLog = { agentId, loading: true, commands: previous?.commands || [], audit: previous?.audit || [], error: "", fetchedAt: previous?.fetchedAt || 0 };
  renderDeviceActivity();
  const query = encodeURIComponent(agentId);
  const [commandResult, auditResult] = await Promise.allSettled([
    api(`/api/commands?agent_id=${query}&limit=100&include_all_active=true`),
    api(`/api/audit?agent_id=${query}&limit=100`),
  ]);
  if (requestId !== state.deviceLogRequestId || authEpoch !== state.authEpoch || state.selectedAgent?.id !== agentId) return;
  const commands = commandResult.status === "fulfilled" && Array.isArray(commandResult.value)
    ? commandResult.value.filter((item) => item?.agent_id === agentId) : previous?.commands || [];
  const audit = auditResult.status === "fulfilled" && Array.isArray(auditResult.value)
    ? auditResult.value.filter((entry) => deviceAuditMatchesAgent(entry, agentId, commands)) : previous?.audit || [];
  const failures = [commandResult, auditResult].filter((item) => item.status === "rejected");
  state.deviceLog = { agentId, loading: false, commands, audit, fetchedAt: Date.now(),
    error: failures.length ? "部分设备日志暂不可用，请稍后重试。" : "" };
  renderDeviceActivity();
}

function minerLogSectionMarkup(agent) {
  const canOperate = ["admin", "operator"].includes(state.user?.role);
  const reason = minerLogUnavailableReason(agent);
  return `<section class="drawer-section device-detail-logs"><div class="drawer-section-head"><div><h4>运行日志</h4><small>仅显示该设备任务与审计；矿工日志按需从 Agent 读取</small></div>${canOperate ? `<div class="miner-log-actions"><button class="btn btn-primary btn-small" type="button" data-live-miner-logs${reason ? ` disabled title="${escapeHtml(reason)}"` : ""}>实时矿工日志</button></div>` : ""}</div><div id="deviceOperationLog">${deviceActivityMarkup(agent)}</div><div class="miner-log-subsection">${reason ? `<div class="miner-log-message warning">${escapeHtml(reason)}</div>` : `<div class="miner-log-empty">打开实时矿工日志可连续查看主挖与副挖的输出；关闭窗口即停止采集。</div>`}</div></section>`;
}

function flightTargetEligibility(agent, sheet) {
  if (!["admin", "operator"].includes(state.user?.role)) return { eligible: false, reason: "需要运维人员或管理员权限", warning: "" };
  if (!sheet) return { eligible: false, reason: "请明确选择飞行表", warning: "" };
  const issue = flightSheetVersionIssue(sheet);
  if (issue) return { eligible: false, reason: issue, warning: "" };
  if (sheet.miner_type === "hiveos") {
    return { eligible: false, reason: "HiveOS 已于控制端 0.8.48 停止支持；此历史飞行表不可下发", warning: "" };
  }
  return { eligible: true, reason: "", warning: "" };
}

function syncDeploySelection() {
  const agent = state.agents.find((item) => item.id === state.selectedAgent?.id) || state.selectedAgent;
  const sheet = state.flightSheets.find((item) => item.id === $("#deploySelect").value);
  const eligibility = state.deployAgentId !== agent?.id
    ? { eligible: false, reason: "目标设备已变化，请重新打开下发窗口", warning: "" }
    : flightTargetEligibility(agent, sheet);
  $("#submitDeployButton").disabled = !eligibility.eligible;
  $("#deployWarning").classList.toggle("hidden", !(eligibility.reason || eligibility.warning));
  $("#deployWarningText").textContent = eligibility.reason || eligibility.warning;
  return eligibility;
}

function miningControlPresentation(agent, status, miningState) {
  if (agent?.status !== "online") {
    return {
      label: "状态未确认",
      className: "disabled",
      stopActive: false,
      stopReason: "设备离线，无法确认矿工进程状态",
    };
  }
  const verifiedRunning = miningProcessIsConfirmed(status, agent);
  const stateValue = String(miningState || "unknown").toLowerCase();
  const states = {
    running: ["进程运行 · 上报有算力", "", true, ""],
    active: ["进程运行 · 上报有算力", "", true, ""],
    degraded: ["部分矿工运行", "warning", true, ""],
    running_zero_hashrate: ["进程运行 · 上报暂无算力", "warning", true, ""],
    running_partial_hashrate: ["部分矿工算力待确认", "warning", true, ""],
    starting: ["正在启动", "warning", true, ""],
    stopping: ["正在停止", "warning", true, ""],
    stopped: ["未挖矿", "disabled", false, "挖矿已经停止"],
    inactive: ["未挖矿", "disabled", false, "挖矿已经停止"],
    not_configured: ["未配置", "disabled", false, "设备尚未应用飞行表"],
    stale: ["未挖矿 · 状态残留", "warning", false, "未检测到矿工进程，仅存在旧的运行状态"],
    failed: ["未挖矿 · 运行失败", "warning", false, "矿工启动失败，当前没有可停止的矿工进程"],
    error: ["未挖矿 · 状态异常", "warning", false, "矿工状态异常，当前没有已确认的矿工进程"],
    unknown: ["状态未知", "disabled", false, "尚未确认矿工进程状态"],
  };
  if (["running_no_stats", "running_attention"].includes(stateValue)) {
    return { label: stateValue === "running_attention" ? "进程运行 · 状态待确认" : `进程运行 · ${miningStatisticsLabel(status, agent)}`,
      className: "warning", stopActive: verifiedRunning, stopReason: verifiedRunning ? "" : "进程证据已过期，请先刷新状态" };
  }
  if (stateValue === "unconfirmed") {
    return verifiedRunning
      ? { label: "运行中 · 算力未确认", className: "warning", stopActive: true, stopReason: "" }
      : { label: "状态未确认", className: "warning", stopActive: false, stopReason: "尚未确认矿工进程正在运行" };
  }
  const [label, className, stopActive, stopReason] = states[stateValue] || states.unknown;
  return { label, className, stopActive, stopReason };
}

function miningControlButtons(agent) {
  const canOperate = state.user?.role === "operator" || state.user?.role === "admin";
  const miningStatus = miningStatusForAgent(agent);
  const configured = miningStatus?.configured === true;
  const miningState = configured ? miningDisplayState(miningStatus, agent) : "not_configured";
  const presentation = miningControlPresentation(agent, miningStatus, miningState);
  const stateTitle = [
    miningStateLabel(miningState),
    ...miningStatusErrorMessages(miningStatus),
  ].filter(Boolean).map(String).join("；");
  const stateMarkup = `<span class="status-pill mining-list-state ${presentation.className}" title="${escapeHtml(stateTitle)}">${escapeHtml(presentation.label)}</span>`;
  if (!canOperate) return stateMarkup;
  const hasPendingControl = state.commands.some((command) => command.agent_id === agent.id
    && ["stop_mining", "restart_mining"].includes(command.action)
    && ["queued", "dispatched", "running"].includes(command.status));
  let unavailable = "";
  if (agent.status !== "online") unavailable = "设备离线，无法下发矿工控制任务";
  else if (!agentSupportsMiningControl(agent.agent_version)) unavailable = "需要 RigPilot Agent 0.5.2 或更高版本";
  else if (!configured) unavailable = "设备尚未应用飞行表";
  else if (hasPendingControl) unavailable = "已有停止或重启挖矿任务正在执行";
  const stopUnavailable = unavailable || (!presentation.stopActive ? presentation.stopReason : "");
  const restartUnavailable = unavailable || (miningStatusUsesHiveos(miningStatus) ? "HiveOS 已停止支持，不能重启该矿工" : "");
  const button = (action, label, disabledReason, className = "") => `<button class="text-button mining-control-button ${className}" type="button" data-mining-action="${action}" data-agent-id="${escapeHtml(agent.id)}" data-agent-name="${escapeHtml(agent.name)}"${disabledReason ? ` disabled title="${escapeHtml(disabledReason)}"` : ""}>${label}</button>`;
  return stateMarkup
    + button("stop_mining", "停止挖矿", stopUnavailable, "danger-text")
    + button("restart_mining", "重启挖矿", restartUnavailable);
}

function hasHashrateAmount(value) {
  return ["number", "string"].includes(typeof value)
    && (typeof value !== "string" || value.trim() !== "")
    && Number.isFinite(Number(value))
    && Number(value) >= 0;
}

function miningHashrateDetails(status) {
  const details = Array.isArray(status?.hashrate_details)
    ? status.hashrate_details.filter((detail) => detail && typeof detail === "object")
    : [];
  const rows = details.slice();
  if (!rows.length) {
    const rates = status?.hashrate;
    if (rates && typeof rates === "object" && !Array.isArray(rates)) {
      for (const role of ["primary", "secondary"]) {
        if (Object.prototype.hasOwnProperty.call(rates, role)) {
          rows.push({ role, hashrate: rates[role] });
        }
      }
      if (!rows.length && ["now", "current", "total", "total_hashrate"].some((key) => Object.prototype.hasOwnProperty.call(rates, key))) {
        rows.push({ role: "primary", hashrate: rates });
      }
    } else if (rates !== undefined && rates !== null) {
      rows.push({ role: "primary", hashrate: rates });
    }
  }
  for (const role of ["primary", "secondary"]) {
    if (status?.[role] && !rows.some((detail, index) => miningHashrateRole(detail, index) === role)) {
      rows.push({ role, hashrate: null });
    }
  }
  return rows;
}

function miningHashrateRole(detail, index) {
  return String(detail.role || (["primary", "secondary"][index] || `algorithm_${index + 1}`)).trim().toLowerCase();
}

function miningHashrateObservedValue(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const key = ["now", "current", "total", "total_hashrate"].find((name) => Object.prototype.hasOwnProperty.call(value, name));
    value = key ? value[key] : null;
  }
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function miningHashrateDetailIsFresh(status, detail, agent = null) {
  // Older Agents only timestamp the aggregate. An explicit per-role timestamp
  // must stand on its own, including null; a fresh CPU sample cannot refresh it.
  if (!Object.prototype.hasOwnProperty.call(detail || {}, "stats_updated_at")) return true;
  const sampledAt = typeof detail.stats_updated_at === "string" ? Date.parse(detail.stats_updated_at) : NaN;
  const checkedAt = Date.parse(status?.checked_at);
  const bootAt = Date.parse(agent?.metrics?.boot_time);
  return Number.isFinite(sampledAt) && Number.isFinite(checkedAt)
    && sampledAt <= checkedAt + 30000 && checkedAt - sampledAt <= 30000
    && (!Number.isFinite(bootAt) || sampledAt >= bootAt);
}

function deviceHashrateRoleLabel(role) {
  const numbered = String(role || "").match(/^algorithm_(\d+)$/);
  return numbered ? `算法 ${numbered[1]}` : "";
}

function deviceMiningMinerIdentity(process, detail, status, role) {
  const hasSecondary = Boolean(status?.secondary)
    || (Array.isArray(status?.hashrate_details) && status.hashrate_details.some((item) => item?.role === "secondary"))
    || hasHashrateAmount(status?.hashrate?.secondary);
  const single = !hasSecondary && role !== "secondary";
  const leg = process || (single ? status?.primary : null);
  const reportedPackage = leg?.miner_package;
  const type = String(detail?.miner_type || leg?.miner_type || (single ? status?.miner_type : "") || "");
  const hiveos = type === "hiveos";
  const name = String(detail?.miner_name || leg?.miner_name || (single ? status?.miner_name : "")
    || reportedPackage?.name || minerPackageTypeLabel(type) || type || "矿工未上报").trim();
  const usable = (value) => typeof value === "string" && value.trim() && value.length <= 120
    && !/[\x00-\x1f\x7f]/.test(value) && !/^(?:sha256[-:]|[a-f0-9]{64}$)/i.test(value.trim())
    && !/^(?:unknown|null|undefined|n\/a|--|未上报)$/i.test(value.trim());
  const runtime = [detail?.version_source === "hiveos_stats" ? detail?.miner_version : "", leg?.miner_version, single ? status?.miner_version : "", detail?.miner_version].find(usable);
  const packageVersion = reportedPackage?.version;
  const version = runtime || (hiveos ? "" : usable(packageVersion) ? packageVersion : "");
  const origin = runtime ? "runtime" : version ? "package" : "unknown";
  return { name: hiveos ? name.replace(/_custom$/i, "") : name, version: String(version).trim(), origin, hiveos };
}

function hashrateToHps(value, unit) {
  if (!hasHashrateAmount(value)) return null;
  const normalizedUnit = String(unit || "H/s").replaceAll(" ", "").toLowerCase();
  const unitIndex = HASHRATE_UNITS.findIndex((candidate) => candidate.toLowerCase() === normalizedUnit);
  return unitIndex < 0 ? null : Number(value) * (1000 ** unitIndex);
}

function fullGpuPciAddress(value) {
  const match = String(value || "").trim().toLowerCase().match(/^([0-9a-f]{4}|[0-9a-f]{8}):([0-9a-f]{2}):([0-9a-f]{2})\.([0-7])$/);
  if (!match) return "";
  const domain = Number.parseInt(match[1], 16);
  if (domain > 65535) return "";
  return `${domain.toString(16).padStart(4, "0")}:${match[2]}:${match[3]}.${match[4]}`;
}

function gpuReportedVendor(gpu) {
  const vendor = String(gpu?.vendor || "").trim().toLowerCase();
  return ["nvidia", "amd"].includes(vendor) ? vendor : "";
}

function gpuDisplayIdentity(gpu) {
  if (gpuReportedVendor(gpu) === "amd") {
    const displayIndex = gpu?.display_index;
    return Number.isInteger(displayIndex) && displayIndex >= 0 ? `GPU ${displayIndex}` : `PCI ${fullGpuPciAddress(gpu?.pci_bus_id) || "未上报"}`;
  }
  const index = gpuMetricIndex(gpu);
  return index === null ? `PCI ${fullGpuPciAddress(gpu?.pci_bus_id) || "未上报"}` : `Index ${index}`;
}

function gpuHashrateReadingIndex(reading) {
  const value = reading?.index;
  const numeric = typeof value === "string" && /^\d+$/.test(value.trim())
    ? Number(value.trim())
    : value;
  return Number.isInteger(numeric) && numeric >= 0 ? numeric : null;
}

function detailSharesGpuIndices(status, detail, indices) {
  const target = new Set(indices);
  const details = Array.isArray(status?.hashrate_details) ? status.hashrate_details : [];
  return details.some((candidate) => candidate !== detail
    && Array.isArray(candidate?.gpu_hashrates)
    && candidate.gpu_hashrates.some((reading) => target.has(gpuHashrateReadingIndex(reading))));
}

function formatHashrateEfficiency(hpsPerWatt) {
  const formatted = formatHashrateAmount(hpsPerWatt, "H/s");
  return formatted === "—" ? "" : formatted.replace(/H\/s$/, "H/s/W");
}

function deviceHashrateEfficiency(agent, status, detail) {
  const readings = Array.isArray(detail?.gpu_hashrates) ? detail.gpu_hashrates : [];
  if (!readings.length) return null;
  const detailHashrateHps = hashrateToHps(
    detail?.hashrate,
    detail?.hashrate_unit || status?.hashrate_unit,
  );
  if (detailHashrateHps === null) return null;
  const indices = readings.map(gpuHashrateReadingIndex);
  if (indices.some((index) => index === null) || new Set(indices).size !== indices.length) return null;
  // A board shared by two algorithm rows has no trustworthy per-algorithm
  // power attribution. Omit the ratio instead of counting the same watts twice.
  if (detailSharesGpuIndices(status, detail, indices)) return null;
  const gpus = Array.isArray(agent?.metrics?.gpus) ? agent.metrics.gpus : [];
  let totalHashrateHps = 0;
  let totalPowerW = 0;
  for (let position = 0; position < readings.length; position += 1) {
    const reading = readings[position];
    const hashrateHps = hashrateToHps(
      reading.hashrate,
      reading.hashrate_unit || detail.hashrate_unit || status?.hashrate_unit,
    );
    const matches = gpus.filter((gpu) => gpuMetricIndex(gpu) === indices[position]);
    if (hashrateHps === null || matches.length !== 1) return null;
    const rawPower = matches[0]?.power_w;
    const powerW = Number(rawPower);
    if (rawPower === null || rawPower === undefined || rawPower === ""
        || typeof rawPower === "boolean" || !Number.isFinite(powerW) || powerW <= 0) return null;
    totalHashrateHps += hashrateHps;
    totalPowerW += powerW;
  }
  if (!Number.isFinite(totalHashrateHps) || !Number.isFinite(totalPowerW) || totalPowerW <= 0) return null;
  // Do not calculate efficiency from a partial per-GPU sample while showing a
  // different total hashrate. Allow only a small rounding tolerance.
  const hashrateDifference = Math.abs(totalHashrateHps - detailHashrateHps);
  const hashrateTolerance = Math.max(1, Math.abs(detailHashrateHps) * 0.02);
  if (hashrateDifference > hashrateTolerance) return null;
  const label = formatHashrateEfficiency(totalHashrateHps / totalPowerW);
  if (!label) return null;
  const powerLabel = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(totalPowerW);
  return {
    label,
    title: `${formatHashrateAmount(totalHashrateHps, "H/s")} ÷ ${powerLabel} W（逐卡 GPU 实时功耗）`,
  };
}

function deviceMiningTableEntry(status, role, value, unit, detail = {}, agent = null) {
  const process = role === "primary" || role === "secondary" ? status?.[role] : null;
  const coin = String(detail.coin || process?.coin || status?.coin || "").trim().toUpperCase();
  const algorithm = String(detail.algorithm || process?.algorithm || status?.algorithm || "").trim();
  const minerIdentity = deviceMiningMinerIdentity(process, detail, status, role);
  return {
    coin: coin || "—",
    algorithm,
    miner: minerIdentity.version && !minerIdentity.name.toLowerCase().includes(minerIdentity.version.toLowerCase())
      ? `${minerIdentity.name} ${minerIdentity.version}` : minerIdentity.name,
    minerIdentity,
    roleLabel: deviceHashrateRoleLabel(role),
    amount: hasHashrateAmount(value) ? formatHashrateAmount(value, unit) : "--",
    hashrateHps: hashrateToHps(value, unit),
    sourceRole: role,
    efficiency: hasHashrateAmount(value) ? deviceHashrateEfficiency(agent, status, detail) : null,
  };
}

function deviceTableMiningSnapshot(agent) {
  const status = miningStatusForAgent(agent);
  if (!status) return { rows: [], placeholder: "--" };
  const miningState = miningDisplayState(status, agent);
  if (agent?.status !== "online") {
    return { rows: [], placeholder: "--" };
  }
  if (status.configured === false || miningState === "not_configured") {
    return { rows: [], placeholder: "未配置" };
  }
  if (["stopped", "inactive", "stale", "failed", "error"].includes(miningState)) {
    return { rows: [], placeholder: "未挖矿" };
  }
  if (miningState && !["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(miningState)) {
    if (["unconfirmed", "running_no_stats", "running_attention"].includes(miningState)) return { rows: deviceMiningIdentityRows(status), placeholder: "--" };
    return { rows: [], placeholder: "--" };
  }
  const details = miningHashrateDetails(status);
  const rows = details.map((detail, index) => deviceMiningTableEntry(
    status,
    miningHashrateRole(detail, index),
    miningHashrateDetailIsFresh(status, detail, agent) ? miningHashrateObservedValue(detail.hashrate) : null,
    detail.hashrate_unit || status.hashrate_unit,
    detail,
    agent,
  ));
  return { rows: rows.length ? rows : deviceMiningIdentityRows(status), placeholder: "--" };
}

function deviceMiningColumnMarkup(snapshot, column) {
  if (!snapshot.rows.length) {
    return `<span class="device-mining-empty device-hashrate-empty">${escapeHtml(snapshot.placeholder)}</span>`;
  }
  const rows = snapshot.rows.map((row) => {
    const identity = [row.roleLabel, row.coin, row.algorithm].filter(Boolean).join(" · ");
    if (column === "coin") {
      return `<span class="device-mining-row device-mining-coin" title="${escapeHtml(identity)}"><strong>${escapeHtml(row.coin)}</strong></span>`;
    }
    if (column === "miner") {
      const miner = row.minerIdentity || { name: row.miner, version: "", origin: "unknown", hiveos: false };
      const version = miner.version || "版本未上报";
      const description = miner.origin === "source" ? "已绑定软件包的来源版本；设备未上报有效运行版本" : miner.origin === "runtime" ? "设备上报的运行或统计版本" : "已绑定软件包版本";
      const versionTitle = `${version}；${description}`;
      return `<span class="device-mining-row device-mining-miner" title="${escapeHtml(miner.name)}"><strong>${escapeHtml(miner.name)}</strong><span class="device-mining-miner-version"><span title="${escapeHtml(versionTitle)}">${escapeHtml(version)}</span>${miner.hiveos ? '<span class="hiveos-badge">HiveOS</span>' : ""}</span></span>`;
    }
    const efficiencyTitle = row.efficiency?.title || "缺少可核验的逐卡功耗，暂不估算能效";
    const efficiencyLabel = row.efficiency?.label || "—";
    return `<span class="device-mining-row device-mining-hashrate" title="${escapeHtml(identity)}"><strong>${escapeHtml(row.amount)}</strong><em title="${escapeHtml(efficiencyTitle)}">能效 ${escapeHtml(efficiencyLabel)}</em></span>`;
  });
  return `<div class="device-mining-list device-mining-${column}-list">${rows.join("")}</div>`;
}


function agentReleaseMatchesDevice(agent, release) {
  if (!agent || release?.deployable !== true || release?.update_protocol !== "agent_update.v1") return false;
  const osValue = String(agent.os_family || "").toLowerCase();
  const os = ["windows", "win", "win32"].includes(osValue)
    ? "windows"
    : ["linux", "ubuntu", "debian"].includes(osValue) ? "linux" : "";
  const platform = String(release.platform || "").toLowerCase();
  if (!os || os !== platform) return false;
  const architecture = String(agent.inventory?.architecture || "").toLowerCase().replaceAll("-", "_");
  const releaseArchitecture = String(release.architecture || "").toLowerCase().replaceAll("-", "_");
  const x64Aliases = ["amd64", "x86_64", "x64"];
  return x64Aliases.includes(architecture) && x64Aliases.includes(releaseArchitecture);
}

function latestCompatibleAgentRelease(agent) {
  return state.agentReleases
    .filter((release) => agentReleaseMatchesDevice(agent, release) && compareAgentVersions(release.version, agent.agent_version) > 0)
    .sort((left, right) => compareAgentVersions(right.version, left.version) || 0)[0] || null;
}

function agentBootstrapLabel(agent) {
  const bootstrap = agent?.inventory?.agent_update_readiness?.bootstrap;
  if (!bootstrap || typeof bootstrap !== "object"
      || bootstrap.schema_version !== 1
      || bootstrap.protocol !== "agent_bootstrap.v1") return "";
  const launcherVersion = Number.isInteger(bootstrap.launcher?.protocol_version)
    ? bootstrap.launcher.protocol_version : null;
  const updaterVersion = Number.isInteger(bootstrap.updater?.protocol_version)
    ? bootstrap.updater.protocol_version : null;
  const components = [
    launcherVersion === null ? "" : `启动器 v${launcherVersion}`,
    updaterVersion === null ? "" : `更新器 v${updaterVersion}`,
  ].filter(Boolean).join(" / ");
  return `Bootstrap v${bootstrap.schema_version}${components ? ` · ${components}` : ""}`;
}

function agentGpuDriverLabel(agent) {
  const gpus = Array.isArray(agent?.metrics?.gpus) ? agent.metrics.gpus : [];
  const versions = [];
  const seen = new Set();
  for (const gpu of gpus) {
    const version = typeof gpu?.driver_version === "string" ? gpu.driver_version.trim() : "";
    const vendor = gpuReportedVendor(gpu);
    const vendorLabel = vendor === "nvidia" ? "NVIDIA" : vendor === "amd" ? "AMD" : "厂商未确认";
    const key = `${vendor}:${version.toLowerCase()}`;
    if (!version || seen.has(key)) continue;
    seen.add(key);
    versions.push(`${vendorLabel} ${version}`);
  }
  return versions.length ? `显卡驱动 ${versions.join(" / ")}` : "";
}

function agentUpdateProgress(agent) {
  if (!agent || state.user?.role !== "admin") return null;
  let pending = state.agentUpdateRequests[agent.id];
  if (pending && pending.authEpoch !== state.authEpoch) {
    delete state.agentUpdateRequests[agent.id];
    pending = null;
  }
  const jobs = Array.isArray(state.agentUpdates) ? state.agentUpdates
    .filter((update) => update?.agent_id === agent.id)
    .sort((left, right) => {
      const byTime = String(right.created_at || "").localeCompare(String(left.created_at || ""));
      return byTime || String(right.command_id || "").localeCompare(String(left.command_id || ""));
    }) : [];
  const latest = jobs[0] || null;
  const age = pending ? Date.now() - pending.startedAt : Infinity;
  if (pending?.status === "requesting") {
    return age < 60_000 ? pending : { ...pending, status: "verifying" };
  }
  if (pending?.status === "queued") {
    const ownJob = jobs.find((job) => job.command_id === pending.commandId);
    const laterJob = latest && latest.command_id !== pending.commandId
      && pending.serverCreatedAt && String(latest.created_at || "") > pending.serverCreatedAt;
    if (ownJob || laterJob) {
      delete state.agentUpdateRequests[agent.id];
      pending = null;
    } else {
      return age < 30 * 60_000 ? pending : { ...pending, status: "verifying" };
    }
  }
  if (pending?.status === "request_failed") {
    if (age < 90_000) return pending;
    delete state.agentUpdateRequests[agent.id];
    pending = null;
  }
  if (pending) delete state.agentUpdateRequests[agent.id];
  if (!latest) return null;
  const active = ["queued", "dispatched", "activating"].includes(latest.status);
  const recordedAt = Date.parse(latest.completed_at || latest.updated_at || latest.created_at || "");
  if (!active && (!Number.isFinite(recordedAt) || Date.now() - recordedAt > 24 * 60 * 60_000)) return null;
  return {
    status: latest.status,
    targetVersion: latest.target_version,
    commandId: latest.command_id,
    error: latest.error || "",
    heartbeatCount: Number(latest.target_heartbeat_count) || 0,
    stalled: latest.status === "activating" && Boolean(latest.stalled_at),
    stalledAt: latest.stalled_at || "",
  };
}

const agentUpdateDismissKey = "rigpilot_agent_update_dismissed_v1";

function agentUpdateIsTerminal(update) {
  return Boolean(update?.commandId)
    && ["succeeded", "failed", "cancelled", "expired"].includes(update.status);
}

function agentUpdateDismissalSignature(update) {
  return JSON.stringify([update.commandId, update.status, update.targetVersion || ""]);
}

function dismissedAgentUpdates() {
  const username = state.user?.username || "";
  const memory = state.agentUpdateDismissals[username] || {};
  try {
    const value = JSON.parse(localStorage.getItem(`${agentUpdateDismissKey}:${username}`) || "{}");
    const stored = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return { ...stored, ...memory };
  } catch {
    return { ...memory };
  }
}

function persistAgentUpdateDismissals(dismissed) {
  const username = state.user?.username || "";
  state.agentUpdateDismissals[username] = dismissed;
  try {
    localStorage.setItem(`${agentUpdateDismissKey}:${username}`, JSON.stringify(dismissed));
  } catch {
    // Keep the signed-in user's preference even if browser storage is unavailable.
  }
}

function agentUpdateIsDismissed(agent, update) {
  return agentUpdateIsTerminal(update)
    && dismissedAgentUpdates()[agent.id] === agentUpdateDismissalSignature(update);
}

function dismissAgentUpdate(agentId, commandId) {
  const agent = state.agents.find((item) => item.id === agentId);
  const update = agentUpdateProgress(agent);
  if (!agentUpdateIsTerminal(update) || update.commandId !== commandId) return;
  const dismissed = dismissedAgentUpdates();
  dismissed[agent.id] = agentUpdateDismissalSignature(update);
  persistAgentUpdateDismissals(dismissed);
  renderDevices();
  if (state.selectedAgent?.id === agent.id) renderDrawer(agent);
}

function dismissibleAgentUpdates() {
  return state.agents.flatMap((agent) => {
    const update = agentUpdateProgress(agent);
    return agentUpdateIsTerminal(update) && !agentUpdateIsDismissed(agent, update)
      ? [{ agent, update }] : [];
  });
}

function bindAgentUpdateDismiss(root = document) {
  $$('[data-dismiss-agent-update]', root).forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    dismissAgentUpdate(button.dataset.dismissAgentUpdate, button.dataset.dismissAgentUpdateCommand);
  }));
}

function agentUpdateStatusMarkup(agent) {
  const update = agentUpdateProgress(agent);
  if (!update || agentUpdateIsDismissed(agent, update)) return "";
  const stalled = update.status === "activating" && update.stalled;
  const text = {
    requesting: "正在提交更新请求",
    queued: "更新排队",
    verifying: "更新状态待核对",
    dispatched: "更新包已下发",
    activating: stalled ? "升级疑似卡滞（新版已回滚或未运行）" : "升级并回连中",
    succeeded: "更新成功",
    failed: "更新失败",
    request_failed: "更新请求失败",
    cancelled: "更新已取消",
    expired: "更新回连超时",
  }[update.status] || "更新状态待确认";
  const target = String(update.targetVersion || "未知版本");
  const detail = update.status === "activating"
    ? stalled
      ? " · 已等待回滚上报或超时清算"
      : ` · 稳定心跳 ${Math.min(update.heartbeatCount || 0, 2)}/2`
    : "";
  // Surface the Agent-reported failure reason (0.8.54+ reports explicit
  // causes such as insufficient disk space) directly in the chip instead of
  // hiding it behind the tooltip, so retries are an informed decision.
  const errorText = ["failed", "request_failed", "expired"].includes(update.status) && update.error
    ? ` · ${update.error}` : "";
  const visibleError = errorText.length > 60 ? `${errorText.slice(0, 60)}…` : errorText;
  const hint = `${text} · 目标 Agent ${target}${detail}${update.error ? ` · ${update.error}` : ""}`;
  const tone = stalled || ["failed", "request_failed", "expired"].includes(update.status) ? "failed"
    : update.status === "succeeded" ? "succeeded" : "progress";
  const dismiss = agentUpdateIsTerminal(update)
    ? `<button type="button" data-dismiss-agent-update="${escapeHtml(agent.id)}" data-dismiss-agent-update-command="${escapeHtml(update.commandId)}" aria-label="关闭${escapeHtml(text)}提示" title="关闭提示">×</button>` : "";
  return `<span class="agent-update-state ${tone}" title="${escapeHtml(hint)}" aria-label="${escapeHtml(hint)}"><span>${escapeHtml(text)} <b>${escapeHtml(target)}</b>${visibleError ? `<small>${escapeHtml(visibleError)}</small>` : ""}</span>${dismiss}</span>`;
}

function agentVersionCellMarkup(agent) {
  const version = escapeHtml(agent.agent_version || "未知");
  const bootstrap = agentBootstrapLabel(agent);
  const gpuDriver = agentGpuDriverLabel(agent);
  const versionMarkup = `<span class="agent-version-cell">Agent ${version}</span>`
    + (bootstrap ? `<span class="agent-bootstrap-version">${escapeHtml(bootstrap)}</span>` : "")
    + (gpuDriver ? `<span class="agent-gpu-driver-version">${escapeHtml(gpuDriver)}</span>` : "");
  const update = agentUpdateProgress(agent);
  if (update && ["requesting", "queued", "verifying", "dispatched", "activating"].includes(update.status)) {
    return `<div class="agent-version-stack">${versionMarkup}<span class="agent-update-target">目标 Agent ${escapeHtml(update.targetVersion || "未知版本")}</span></div>`;
  }
  const release = state.user?.role === "admin" ? latestCompatibleAgentRelease(agent) : null;
  if (!release) return bootstrap || gpuDriver
    ? `<div class="agent-version-stack">${versionMarkup}</div>`
    : versionMarkup;
  const eligibility = agentUpdateEligibility(agent, release);
  if (!eligibility.eligible) {
    const blocker = eligibility.reason.includes("不在线")
      ? "设备离线"
      : eligibility.reason.includes("手动安装") || eligibility.reason.includes("完整包")
        ? "需手动升级"
        : eligibility.reason.includes("agent_update.v1")
          ? (String(agent.os_family || "").toLowerCase().startsWith("win")
            ? "需修复本地更新能力"
            : "需安装完整包")
          : "暂不可更新";
    return '<div class="agent-version-stack">' + versionMarkup
      + '<span class="agent-update-needed is-disabled" title="' + escapeHtml(eligibility.reason)
      + '">需要更新至 ' + escapeHtml(release.version) + ' · ' + escapeHtml(blocker) + "</span></div>";
  }
  return '<div class="agent-version-stack">' + versionMarkup
    + '<button class="text-button agent-update-needed" type="button" data-agent-update-release="'
      + escapeHtml(release.id) + '" data-agent-update-agent="' + escapeHtml(agent.id)
      + '">需要更新至 ' + escapeHtml(release.version) + "</button></div>";
}

function gpuMiningHashrateSnapshot(agent, gpu) {
  const index = gpuMetricIndex(gpu);
  const status = miningStatusForAgent(agent);
  const active = (index != null || (gpuReportedVendor(gpu) === "amd" && Boolean(fullGpuPciAddress(gpu?.pci_bus_id))))
    && agent?.status === "online"
    && status
    && ["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(miningDisplayState(status, agent));
  const readings = [];
  if (!active) return { active: false, status, readings };

  const details = Array.isArray(status.hashrate_details)
    ? status.hashrate_details.filter((detail) => detail && typeof detail === "object")
    : [];
  let sequence = 0;
  for (const detail of details) {
    if (!miningHashrateDetailIsFresh(status, detail, agent)) continue;
    const role = String(detail.role || "");
    const process = ["primary", "secondary"].includes(role)
      && status?.[role]
      && typeof status[role] === "object"
      ? status[role]
      : null;
    const minerType = String(
      detail.miner_type
      || process?.miner_type
      || (role === "primary" ? status.miner_type : "")
      || "",
    ).trim().toLowerCase();
    const algorithm = String(detail.algorithm || "").trim().toLowerCase();
    if (["randomx", "rx", "rx/0", "rx0"].includes(algorithm)) continue;
    const authority = ["srbminer", "pearlminer"].includes(minerType)
      ? 2
      : minerType ? 0 : 1;
    if (!authority) continue;
    const gpuHashrates = Array.isArray(detail.gpu_hashrates) ? detail.gpu_hashrates : [];
    for (const reading of gpuHashrates) {
      if (!reading || typeof reading !== "object") continue;
      if (!miningHashrateDetailIsFresh(status, reading, agent)) continue;
      const readingIndex = typeof reading.index === "string" && /^\d+$/.test(reading.index)
        ? Number(reading.index)
        : reading.index;
      if (!hasHashrateAmount(reading.hashrate)) continue;
      const hashrate = Number(reading.hashrate);
      if (readingIndex !== index || !Number.isFinite(hashrate) || hashrate < 0) continue;
      const observedAt = Date.parse(
        readingTimestamp(reading)
        || readingTimestamp(detail)
        || status.stats_updated_at
        || status.checked_at
        || "",
      );
      const identity = [detail.coin, detail.algorithm].filter(Boolean).join(" · ")
        || "GPU 算力";
      readings.push({
        identity,
        authority,
        observedAt: Number.isFinite(observedAt) ? observedAt : Number.NEGATIVE_INFINITY,
        sequence: sequence++,
        amount: formatHashrateAmount(
          hashrate,
          reading.hashrate_unit || detail.hashrate_unit || status.hashrate_unit,
        ),
      });
    }
  }
  return { active: true, status, readings };
}

function readingTimestamp(detail) {
  return [
    detail?.sampled_at,
    detail?.updated_at,
    detail?.stats_updated_at,
  ].find((value) => typeof value === "string" && value.trim()) || "";
}

function gpuMiningHashrateTileReading(agent, gpu) {
  return gpuMiningHashrateSnapshot(agent, gpu).readings.reduce((selected, reading) => {
    if (!selected) return reading;
    if (reading.authority !== selected.authority) {
      return reading.authority > selected.authority ? reading : selected;
    }
    if (reading.observedAt !== selected.observedAt) {
      return reading.observedAt > selected.observedAt ? reading : selected;
    }
    return reading.sequence > selected.sequence ? reading : selected;
  }, null);
}

function gpuTileHashrateAmountMarkup(amount) {
  const value = String(amount || "").trim();
  const parts = value.match(/^(.+?)\s+([kMGTPE]?H\/s)$/i);
  if (!parts) return escapeHtml(value);
  return `${escapeHtml(parts[1])}<span class="gpu-temperature-hashrate-unit">`
    + `${escapeHtml(parts[2])}</span>`;
}

function gpuDriverUnavailable(gpu) {
  return gpu?.driver_available === false
    && gpu?.error === "nvidia_driver_unavailable";
}

function gpuTileModelLabel(name, index) {
  const fullName = String(name || "").trim().replace(/\s+/g, " ");
  if (!fullName) return `GPU ${index ?? "—"}`;
  const model = fullName
    .replace(/^(?:NVIDIA(?: Corporation)?\s+)?(?:GeForce\s+|Tesla\s+|Quadro\s+)?/i, "")
    .replace(/^RTX\s+/i, "")
    .replace(/^(?:AMD|ATI)\s+Radeon\s+(?=RX\b)/i, "")
    .replace(/^AMD\s+/i, "")
    .replace(/^Intel(?:\(R\))?\s+(?:Corporation\s+)?/i, "")
    .replace(/\((?:R|TM)\)/gi, "")
    .replace(/\s+(?:Graphics|GPU|Series)$/i, "")
    .replace(/\s+/g, " ")
    .trim();
  return model || fullName;
}

function gpuUtilizationPlaceholderMarkup(reason) {
  return `<span class="gpu-utilization-placeholder" role="img" aria-label="${escapeHtml(reason)}" title="${escapeHtml(reason)}">—</span>`;
}

function gpuUtilizationTableMarkup(agent) {
  const reported = Array.isArray(agent.metrics?.gpus) ? agent.metrics.gpus : null;
  const gpus = reported ? reported.filter((gpu) => gpu && typeof gpu === "object") : [];
  if (!gpus.length) return gpuUtilizationPlaceholderMarkup(reported === null
    ? "GPU数据暂未上报" : reported.length ? "GPU上报数据暂不可用" : "设备当前未上报GPU");
  return '<div class="gpu-utilization-list">' + gpus.map((gpu) => {
    const index = gpuMetricIndex(gpu);
    const fullName = String(gpu.name || "").trim();
    const modelMarkup = `<small class="gpu-model-label" title="${escapeHtml(fullName || "型号未上报")}">`
      + `${escapeHtml(gpuTileModelLabel(fullName, index))}</small>`;
    if (gpuDriverUnavailable(gpu)) {
      const pciBus = String(gpu.pci_bus_id || "").trim() || "PCI 地址未上报";
      const tooltip = `${fullName || "NVIDIA GPU"} · ${pciBus} · NVIDIA 驱动未能绑定`;
      return '<div class="gpu-utilization-row" title="' + escapeHtml(tooltip)
        + '" aria-label="' + escapeHtml(tooltip)
        + '">' + modelMarkup + '<em class="gpu-temperature-badge is-error">驱动异常</em></div>';
    }
    const fanSpeedRaw = gpu.fan_percent ?? gpu.fan_speed_percent ?? gpu.fan_speed;
    const normalizedFanSpeed = typeof fanSpeedRaw === "string"
      ? fanSpeedRaw.trim().replace(/%$/, "")
      : fanSpeedRaw;
    const fanSpeed = Number(normalizedFanSpeed);
    const fanSpeedLabel = fanSpeedRaw !== null
      && fanSpeedRaw !== undefined
      && fanSpeedRaw !== ""
      && typeof fanSpeedRaw !== "boolean"
      && Number.isFinite(fanSpeed)
      ? `${Math.round(fanSpeed)}%`
      : "未上报";
    const temperature = Number(gpu.temperature_c);
    const hasTemperature = gpu.temperature_c !== null
      && gpu.temperature_c !== undefined
      && gpu.temperature_c !== ""
      && typeof gpu.temperature_c !== "boolean"
      && Number.isFinite(temperature);
    const temperatureLabel = hasTemperature ? `${Math.round(temperature)}°C` : "—";
    const temperatureState = !hasTemperature ? "is-missing" : temperature > 75 ? "is-hot" : "is-normal";
    const hashrate = gpuMiningHashrateTileReading(agent, gpu);
    const hashrateMarkup = hashrate
      ? `<span class="gpu-temperature-hashrates"><small class="gpu-temperature-hashrate" title="${escapeHtml(hashrate.identity)}">`
        + `${gpuTileHashrateAmountMarkup(hashrate.amount)}</small></span>`
      : "";
    const hashrateLabel = hashrate
      ? ` · 算力 ${hashrate.identity} ${hashrate.amount}`
      : "";
    const tooltip = `GPU ${index ?? "—"} · ${fullName || "型号未上报"} · 风扇 ${fanSpeedLabel}${hashrateLabel}`;
    return '<div class="gpu-utilization-row" title="' + escapeHtml(tooltip) + '" aria-label="'
      + escapeHtml(tooltip) + '">' + modelMarkup + '<em class="gpu-temperature-badge ' + temperatureState + '">'
      + temperatureLabel + `</em>${hashrateMarkup}</div>`;
  }).join("") + "</div>";
}

const configurationRefreshDismissKey = "rigpilot_configuration_refresh_dismissed_v1";

function configurationRefreshSignature(refresh) {
  return [
    refresh?.status,
    refresh?.updated_at,
    refresh?.completed_at,
    refresh?.command_id,
    refresh?.desired_fingerprint,
  ].map((value) => String(value || "")).join("|");
}

function dismissedConfigurationRefreshes() {
  try {
    const value = JSON.parse(localStorage.getItem(configurationRefreshDismissKey) || "{}");
    const stored = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return { ...stored, ...state.configurationRefreshDismissals };
  } catch {
    return { ...state.configurationRefreshDismissals };
  }
}

function persistConfigurationRefreshDismissals(dismissed) {
  state.configurationRefreshDismissals = dismissed;
  try {
    localStorage.setItem(configurationRefreshDismissKey, JSON.stringify(dismissed));
  } catch {
    // Private browsing may reject durable storage; the in-memory preference
    // still hides the badge for the current signed-in session.
  }
}

function configurationRefreshIsDismissed(agent) {
  const refresh = agent?.configuration_refresh;
  if (String(refresh?.status || "").toLowerCase() !== "succeeded") return false;
  return dismissedConfigurationRefreshes()[agent.id] === configurationRefreshSignature(refresh);
}

function dismissConfigurationRefresh(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent || String(agent.configuration_refresh?.status || "").toLowerCase() !== "succeeded") return;
  const dismissed = dismissedConfigurationRefreshes();
  dismissed[agent.id] = configurationRefreshSignature(agent.configuration_refresh);
  persistConfigurationRefreshDismissals(dismissed);
  renderDevices();
  if (state.selectedAgent?.id === agent.id) renderDrawer(agent);
}

function dismissibleConfigurationRefreshAgents() {
  const dismissed = dismissedConfigurationRefreshes();
  return state.agents.filter((agent) => {
    const refresh = agent?.configuration_refresh;
    return String(refresh?.status || "").toLowerCase() === "succeeded"
      && dismissed[agent.id] !== configurationRefreshSignature(refresh);
  });
}

function syncConfigurationRefreshClearButton() {
  const button = $("#clearConfigurationRefreshBadgesButton");
  if (!button) return;
  const count = dismissibleConfigurationRefreshAgents().length + dismissibleAgentUpdates().length;
  button.disabled = count === 0;
  button.textContent = count ? `清除已完成提示 (${count})` : "清除已完成提示";
  button.setAttribute(
    "aria-label",
    count ? `清除全部 ${count} 条已完成提示` : "没有可清除的已完成提示",
  );
}

function dismissAllConfigurationRefreshes() {
  const agents = dismissibleConfigurationRefreshAgents();
  const updates = dismissibleAgentUpdates();
  if (!agents.length && !updates.length) {
    syncConfigurationRefreshClearButton();
    return;
  }
  const dismissed = dismissedConfigurationRefreshes();
  for (const agent of agents) {
    dismissed[agent.id] = configurationRefreshSignature(agent.configuration_refresh);
  }
  if (agents.length) persistConfigurationRefreshDismissals(dismissed);
  if (updates.length) {
    const updateDismissals = dismissedAgentUpdates();
    for (const { agent, update } of updates) {
      updateDismissals[agent.id] = agentUpdateDismissalSignature(update);
    }
    persistAgentUpdateDismissals(updateDismissals);
  }
  renderDevices();
  if (state.selectedAgent) renderDrawer(state.selectedAgent);
  toast(`已清除 ${agents.length + updates.length} 条已完成提示`);
}

function bindConfigurationRefreshDismiss(root = document) {
  $$('[data-dismiss-configuration-refresh]', root).forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    dismissConfigurationRefresh(button.dataset.dismissConfigurationRefresh);
  }));
}

function configurationRefreshBadge(agent, detailed = false) {
  const refresh = agent?.configuration_refresh;
  if (!refresh || typeof refresh !== "object") return "";
  const status = String(refresh.status || "").toLowerCase();
  if (status === "succeeded" && configurationRefreshIsDismissed(agent)) return "";
  const display = {
    pending: ["配置待更新", "pending"],
    preparing: ["配置下发中", "progress"],
    queued: ["配置下发中", "progress"],
    dispatched: ["配置下发中", "progress"],
    running: ["配置下发中", "progress"],
    succeeded: ["配置已更新", "success"],
    failed: ["配置更新失败", "failed"],
    cancelled: ["配置更新失败", "failed"],
    expired: ["配置更新失败", "failed"],
  }[status];
  if (!display) return "";
  const pendingHint = agent.status === "offline"
    ? "设备上线并开始挖矿后自动下发最新配置"
    : "等待设备进入挖矿状态后下发最新配置";
  const hint = status === "pending"
    ? pendingHint
    : status === "succeeded" ? "设备已应用最新钱包和飞行表配置"
      : refresh.last_error || display[0];
  const dismiss = status === "succeeded"
    ? `<button type="button" data-dismiss-configuration-refresh="${escapeHtml(agent.id)}" aria-label="关闭配置已更新提示" title="关闭提示">×</button>`
    : "";
  return `<span class="configuration-refresh-badge ${display[1]}${detailed ? " detailed" : ""}" title="${escapeHtml(hint)}"><span>${escapeHtml(display[0])}</span>${dismiss}</span>`;
}


function deviceStatusCellMarkup(agent, showConfigurationRefresh = false) {
  const lastSeen = formatTime(agent.last_seen);
  const absolute = formatTime(agent.last_seen, false);
  return `<div class="device-status-stack"><span class="status-pill ${escapeHtml(agent.status)}">${escapeHtml(statusLabel(agent.status))}</span><small class="device-last-seen" title="最后在线：${escapeHtml(absolute)}" aria-label="最后在线：${escapeHtml(absolute)}">${escapeHtml(lastSeen)}</small>${agentUpdateStatusMarkup(agent)}${showConfigurationRefresh ? configurationRefreshBadge(agent) : ""}</div>`;
}

function deviceTableRow(agent) {
  const os = osLabel(agent);
  const metrics = agent.metrics || {};
  const resourcesOnline = agent.status === "online";
  const miningSnapshot = deviceTableMiningSnapshot(agent);
  const usage = (value, detail = "") => `<div class="usage"><strong${detail ? ' class="usage-summary"' : ""}>${metric(value)}${detail ? `<em title="${escapeHtml(detail)}">· ${escapeHtml(detail)}</em>` : ""}</strong><div class="usage-line"><i style="width:${clampPercent(value)}%"></i></div></div>`;
  const cpuCell = resourcesOnline ? usage(metrics.cpu_percent, cpuTemperatureLabel(metrics)) : "-";
  const memoryCell = resourcesOnline ? usage(metrics.memory_percent) : "-";
  const gpuCell = resourcesOnline ? gpuUtilizationTableMarkup(agent)
    : gpuUtilizationPlaceholderMarkup("设备离线，GPU状态未确认");
  return `<tr data-agent-id="${escapeHtml(agent.id)}">
    <td class="fleet-col-device"><div class="device-ident"><span class="os-badge ${os.className}">${os.short}</span><div><strong>${escapeHtml(agent.name)}</strong></div></div></td>
    <td class="fleet-col-status">${deviceStatusCellMarkup(agent, true)}</td>
    <td class="fleet-col-coin">${deviceMiningColumnMarkup(miningSnapshot, "coin")}</td>
    <td class="fleet-col-miner">${deviceMiningColumnMarkup(miningSnapshot, "miner")}</td>
    <td class="fleet-col-hashrate">${deviceMiningColumnMarkup(miningSnapshot, "hashrate")}</td>
    <td class="fleet-col-cpu">${cpuCell}</td><td class="fleet-col-memory">${memoryCell}</td><td class="fleet-col-gpu">${gpuCell}</td>
    <td class="fleet-col-agent">${agentVersionCellMarkup(agent)}</td><td class="fleet-col-actions"><div class="table-row-actions">${miningControlButtons(agent)}${state.user?.role === "admin" && agent.status === "offline" ? `<button class="text-button danger-text" type="button" data-delete-agent="${escapeHtml(agent.id)}" data-agent-name="${escapeHtml(agent.name)}">删除</button>` : ""}<span class="row-arrow">›</span></div></td>
  </tr>`;
}

function setTableVisible(table, visible) {
  if (!table) return;
  table.classList.toggle("hidden", !visible);
  table.closest(".table-wrap")?.classList.toggle("hidden", !visible);
}

function overviewFreshOnlineAgent(agent) {
  const heartbeatAt = Date.parse(agent?.last_seen);
  return agent?.status === "online" && Number.isFinite(heartbeatAt)
    && Math.abs(Date.now() - heartbeatAt) <= 120000;
}

function overviewGpuTelemetry(agent) {
  const result = { total: 0, powerCount: 0, powerW: 0, temperatures: [], fans: [] };
  if (!overviewFreshOnlineAgent(agent)) return result;
  const gpus = Array.isArray(agent?.metrics?.gpus) ? agent.metrics.gpus : [];
  const seen = new Set();
  for (const [position, gpu] of gpus.entries()) {
    if (!gpu || typeof gpu !== "object") continue;
    const identity = gpuMetricUuid(gpu) || fullGpuPciAddress(gpu.pci_bus_id)
      || `index:${gpuMetricIndex(gpu) ?? position}`;
    if (seen.has(identity)) continue;
    seen.add(identity);
    result.total += 1;
    const rawPower = gpu.power_w;
    const powerW = Number(rawPower);
    if (rawPower !== null && rawPower !== undefined && rawPower !== ""
        && typeof rawPower !== "boolean" && Number.isFinite(powerW) && powerW >= 0) {
      result.powerW += powerW;
      result.powerCount += 1;
    }
    const temperature = Number(gpu.temperature_c);
    if (gpu.temperature_c !== null && gpu.temperature_c !== undefined && gpu.temperature_c !== ""
        && Number.isFinite(temperature) && temperature >= -20 && temperature <= 150) {
      result.temperatures.push(temperature);
    }
    const fan = Number(gpu.fan_speed_percent);
    if (gpu.fan_speed_percent !== null && gpu.fan_speed_percent !== undefined && gpu.fan_speed_percent !== ""
        && Number.isFinite(fan) && fan >= 0 && fan <= 100) {
      result.fans.push(fan);
    }
  }
  return result;
}

function overviewFormatPower(watts) {
  if (!Number.isFinite(watts) || watts < 0) return "—";
  if (watts >= 1000) return `${(watts / 1000).toFixed(2)} kW`;
  return `${Math.round(watts)} W`;
}

function overviewHashrateGroups(agents = state.agents) {
  const groups = new Map();
  let missingOnline = 0;
  for (const agent of agents) {
    if (!overviewFreshOnlineAgent(agent)) continue;
    const rows = deviceTableMiningSnapshot(agent).rows;
    const seen = new Set();
    let reported = false;
    for (const row of rows) {
      const hps = row.hashrateHps;
      const coin = String(row.coin || "").trim().toUpperCase();
      const algorithm = String(row.algorithm || "").trim().toLowerCase();
      const role = String(row.sourceRole || "").trim().toLowerCase();
      if (!Number.isFinite(hps) || hps < 0 || !coin || coin === "—" || coin === "UNKNOWN") continue;
      if (rows.length > 1 && ["total", "aggregate", "all"].includes(role)) continue;
      const readingKey = `${role}\u0000${coin}\u0000${algorithm}`;
      if (seen.has(readingKey)) continue;
      seen.add(readingKey);
      const group = groups.get(coin) || { coin, algorithms: new Set(), hps: 0, agentIds: new Set() };
      group.hps += hps;
      if (algorithm) group.algorithms.add(algorithm);
      group.agentIds.add(agent.id);
      groups.set(coin, group);
      reported = true;
    }
    if (!reported) missingOnline += 1;
  }
  return {
    groups: [...groups.values()].sort((left, right) => left.coin.localeCompare(right.coin)),
    missingOnline,
  };
}

function renderOverviewAggregateSummary() {
  const { groups, missingOnline } = overviewHashrateGroups();
  const breakdown = $("#summaryHashrateGroups");
  if (groups.length) {
    breakdown.innerHTML = groups.map((group) => {
      const algorithms = [...group.algorithms].sort().join(" / ");
      const detail = [
        group.coin,
        formatHashrateAmount(group.hps, "H/s"),
        algorithms ? `算法 ${algorithms}` : "",
        `${group.agentIds.size} 台在线设备`,
        missingOnline ? `${missingOnline} 台在线设备无可用统计` : "",
      ].filter(Boolean).join(" · ");
      return `<span role="listitem" title="${escapeHtml(detail)}"><b>${escapeHtml(group.coin)}</b><strong>${escapeHtml(formatHashrateAmount(group.hps, "H/s"))}</strong></span>`;
    }).join("");
  } else {
    const reason = state.agents.some(overviewFreshOnlineAgent)
      ? "在线设备尚无可用的新鲜币种算力" : "暂无在线设备或新鲜算力统计";
    breakdown.innerHTML = `<span class="summary-hashrate-empty" role="listitem" title="${escapeHtml(reason)}">—</span>`;
  }
  const telemetry = state.agents.map(overviewGpuTelemetry);
  const known = telemetry.reduce((total, item) => total + item.powerCount, 0);
  const total = telemetry.reduce((sum, item) => sum + item.total, 0);
  const watts = telemetry.reduce((sum, item) => sum + item.powerW, 0);
  $("#summaryGpuPower").textContent = known ? overviewFormatPower(watts) : "—";
  $("#summaryGpuPowerNote").textContent = known
    ? `${known} 张在线 GPU 已上报${total > known ? `，${total - known} 张缺失` : ""}；不含 CPU/主机`
    : "暂无在线 GPU 功耗上报；不含 CPU/主机";
}

function overviewFilteredAgents() {
  const query = $("#overviewDeviceSearch")?.value.trim().toLowerCase() || "";
  return state.agents.filter((agent) => {
    const os = String(agent.os_family || "").trim().toLowerCase();
    if (state.overviewOsFilter !== "all" && os !== state.overviewOsFilter) return false;
    if (!query) return true;
    const mining = deviceTableMiningSnapshot(agent);
    const searchable = [agent.name, agent.note, agent.hostname, agent.os_family, agent.platform,
      agent.public_ip, agent.local_ip, ...(agent.local_ip_addresses || []),
      ...mining.rows.flatMap((row) => [row.coin, row.algorithm, row.minerIdentity?.name])]
      .join(" ").toLowerCase();
    return searchable.includes(query);
  });
}

function overviewTemperatureMarkup(agent) {
  const metrics = agent?.metrics && typeof agent.metrics === "object" ? agent.metrics : {};
  const reportedGpus = metrics.gpus;
  const fresh = overviewFreshOnlineAgent(agent);
  const chip = (label, kind, description, cpu = false) => `<span class="overview-temperature-chip ${kind}${cpu ? " is-cpu" : ""}" role="img" title="${escapeHtml(description)}" aria-label="${escapeHtml(description)}">${escapeHtml(label)}</span>`;
  if (Array.isArray(reportedGpus)) {
    if (reportedGpus.length) return `<div class="overview-temperature-list">${Array.from(reportedGpus, (reportedGpu, position) => {
      const gpu = reportedGpu && typeof reportedGpu === "object" ? reportedGpu : null;
      const index = gpuMetricIndex(gpu);
      const identity = `GPU ${index ?? position} · ${String(gpu?.name || gpu?.model || "型号未上报").trim()}`;
      if (!gpu) return chip("—", "is-missing", `${identity} · GPU 数据未确认`);
      if (!fresh) return chip("—", "is-missing", `${identity} · 设备离线或心跳过期，温度未确认`);
      if (gpuDriverUnavailable(gpu) || gpu.driver_available === false) return chip("!", "is-error", `${identity} · 显卡驱动异常，温度未确认`);
      const raw = gpu.temperature_c;
      const numeric = Number(raw);
      const valid = raw !== null && raw !== undefined && raw !== "" && typeof raw !== "boolean"
        && Number.isFinite(numeric) && numeric >= -40 && numeric <= 150;
      if (!valid) return chip("—", "is-missing", `${identity} · GPU 温度未上报`);
      return chip(String(Math.round(numeric)), numeric >= 75 ? "is-hot" : "is-normal", `${identity} · 实际温度 ${numeric}°C`);
    }).join("")}</div>`;
    const cpu = fresh ? cpuTemperatureC(metrics) : null;
    if (cpu === null) return chip("—", "is-missing", fresh ? "未检测到 GPU；CPU 温度未上报" : "设备离线或心跳过期，CPU 温度未确认", true);
    return chip(String(Math.round(cpu)), cpu >= 75 ? "is-hot" : "is-normal", `未检测到 GPU；实际 CPU 温度 ${cpu}°C`, true);
  }
  return chip("—", "is-missing", "GPU 清单未上报；不能确认是否为 CPU-only 设备");
}

function overviewDeviceRow(agent) {
  const os = osLabel(agent);
  const mining = deviceTableMiningSnapshot(agent);
  const gpu = overviewGpuTelemetry(agent);
  const power = gpu.powerCount ? overviewFormatPower(gpu.powerW) : "—";
  return `<tr data-agent-id="${escapeHtml(agent.id)}">
    <td><div class="device-ident"><span class="os-badge ${os.className}">${os.short}</span><div><strong>${escapeHtml(agent.name)}</strong></div></div></td>
    <td>${deviceStatusCellMarkup(agent)}</td>
    <td>${deviceMiningColumnMarkup(mining, "coin")}</td>
    <td>${deviceMiningColumnMarkup(mining, "miner")}</td>
    <td class="overview-col-temperature">${overviewTemperatureMarkup(agent)}</td>
    <td>${deviceMiningColumnMarkup(mining, "hashrate")}</td>
    <td title="在线设备已上报 GPU 功耗合计，不含 CPU/主机">${power}</td>
  </tr>`;
}

function renderOverviewDevices() {
  const body = $("#overviewDeviceTableBody");
  if (!body) return;
  const agents = overviewFilteredAgents();
  body.innerHTML = agents.map(overviewDeviceRow).join("");
  $("#overviewDeviceEmpty").classList.toggle("hidden", agents.length > 0);
  setTableVisible(body.closest("table"), agents.length > 0);
  bindAgentRows($$('tr[data-agent-id]', body));
  bindAgentUpdateDismiss(body);
  const counts = { all: state.agents.length, linux: 0, windows: 0 };
  state.agents.forEach((agent) => {
    const os = String(agent.os_family || "").trim().toLowerCase();
    if (os === "linux" || os === "windows") counts[os] += 1;
  });
  $$('[data-overview-os]').forEach((button) => {
    const os = button.dataset.overviewOs;
    const active = os === state.overviewOsFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.textContent = `${{ all: "全部设备", linux: "Linux", windows: "Windows" }[os]} (${counts[os]})`;
  });
}

function renderDevices() {
  const agents = filteredAgents();
  $("#deviceTableBody").innerHTML = agents.map(deviceTableRow).join("");
  $("#deviceEmpty").classList.toggle("hidden", agents.length > 0);
  setTableVisible($("#deviceTableBody").closest("table"), agents.length > 0);
  bindAgentRows($$('tr[data-agent-id]', $("#deviceTableBody")));
  $$('[data-delete-agent]', $("#deviceTableBody")).forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    deleteOfflineAgent(button.dataset.deleteAgent, button.dataset.agentName);
  }));
  $$('[data-mining-action]', $("#deviceTableBody")).forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    controlMining(button.dataset.miningAction, button.dataset.agentId, button.dataset.agentName, button);
  }));

  $$('[data-agent-update-release]', $("#deviceTableBody")).forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openAgentUpdateDialog(button.dataset.agentUpdateRelease, button.dataset.agentUpdateAgent);
  }));
  bindConfigurationRefreshDismiss($("#deviceTableBody"));
  bindAgentUpdateDismiss($("#deviceTableBody"));
  syncConfigurationRefreshClearButton();
  renderOverviewDevices();
}

async function controlRemoteCommand(commandId, operation, commandName, button) {
  if (!commandId || !["cancel", "abandon"].includes(operation)) return;
  const displayName = commandName || "远程任务";
  const runtimeCancel = operation === "cancel"
    && ["dispatched", "running"].includes(button.dataset.commandState || "");
  const question = operation === "cancel"
    ? runtimeCancel
      ? `确认取消执行中的“${displayName}”任务？\n\n取消将停止服务端任务并释放操作通道；若设备端已卡死（如 GPU 驱动无响应），可能仍需重启设备恢复。`
      : `确认取消尚未被设备领取的“${displayName}”任务？`
    : `确认结束“${displayName}”的离线遗留记录？\n\n这只会结束控制端记录，无法保证设备上已经开始的动作被中断。`;
  if (!window.confirm(question)) return;
  button.disabled = true;
  try {
    const suffix = operation === "cancel" ? "cancel" : "abandon";
    await api(`/api/commands/${encodeURIComponent(commandId)}/${suffix}`, { method: "POST" });
    toast(operation === "cancel"
      ? (runtimeCancel ? "执行中任务已取消（服务端）；设备端如仍卡死请重启设备" : "等待任务已取消")
      : "离线遗留任务记录已结束");
    if (state.selectedAgent?.id) await loadDeviceActivity(state.selectedAgent.id, { force: true });
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function clearAgentUpdateHistory() {
  if (state.user?.role !== "admin") return;
  if (!window.confirm("确认清理已完成的被控端更新记录？\n\n进行中的更新不会被删除；历史记录清理后无法恢复。")) return;
  const button = $("#clearAgentUpdateHistoryButton");
  button.disabled = true;
  try {
    await api("/api/agent-updates/history", { method: "DELETE" });
    toast("被控端更新历史已清理");
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function validSetupState(value) {
  return value && ["pending", "deferred", "complete"].includes(value.status)
    && ["password", "device", "configuration", "done"].includes(value.step)
    && typeof value.password_changed === "boolean" && Number.isSafeInteger(value.revision) && value.revision >= 0;
}

function renderSetupWizard() {
  const data = state.setupState;
  const busy = state.setupLoading || state.setupSaving;
  const step = ["password", "device", "configuration"].includes(data?.step) ? data.step
    : data?.password_changed === true ? "device" : "password";
  const statuses = { pending: "安装向导尚未完成", deferred: "已选择稍后继续", complete: "安装向导已完成" };
  $("#setupStatus").textContent = state.setupError || (state.setupLoading ? "正在读取安装向导进度…" : statuses[data?.status] || "可打开向导查看安装步骤");
  $("#setupError").textContent = state.setupError;
  $("#setupControllerAddress").textContent = String(window.location?.origin || "当前地址暂不可用");
  $("#setupPasswordState").textContent = data?.password_changed === true
    ? "已在安装向导中完成密码修改。" : "建议先更改管理员密码，使用自己保管的密码登录控制端。";
  $("#setupPasswordProgress").textContent = data?.password_changed === true ? "已修改" : "未修改";
  $$("[data-setup-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.setupPanel !== step));
  $$("[data-setup-step]").forEach((button) => {
    button.classList.toggle("is-current", button.dataset.setupStep === step);
    button.setAttribute("aria-current", button.dataset.setupStep === step ? "step" : "false");
  });
  $$("button", $("#setupDialog")).forEach((button) => { button.disabled = Boolean(busy); });
  $("#openSetupButton").disabled = Boolean(busy);
  $("#setupPreviousButton").disabled = Boolean(busy || step === "password");
  $("#setupNextButton").textContent = step === "configuration" ? "完成向导"
    : step === "password" && data?.password_changed !== true ? "暂时跳过" : "下一步";
  $("#setupNextButton").classList.toggle("btn-primary", step !== "password" || data?.password_changed === true);
  $("#setupNextButton").classList.toggle("btn-secondary", step === "password" && data?.password_changed !== true);
  $("#setupDialog").setAttribute("aria-busy", String(Boolean(busy)));
}

async function loadSetupState({ autoOpen = false } = {}) {
  if (state.user?.role !== "admin" || !state.token) return false;
  const requestId = ++state.setupRequestId;
  const authEpoch = state.authEpoch, token = state.token;
  const stillCurrent = () => requestId === state.setupRequestId && authEpoch === state.authEpoch
    && token === state.token && state.user?.role === "admin";
  state.setupLoading = true;
  state.setupError = "";
  renderSetupWizard();
  try {
    const result = await api("/api/setup");
    if (!stillCurrent()) return false;
    if (!validSetupState(result)) throw new Error("Setup response not confirmed");
    state.setupState = result;
    renderSetupWizard();
    if (autoOpen && result.status === "pending" && !$("#setupDialog").open) $("#setupDialog").showModal();
    return true;
  } catch {
    if (!stillCurrent()) return false;
    state.setupError = "安装向导暂时无法读取，请稍后在系统设置重试。";
    return false;
  } finally {
    if (stillCurrent()) { state.setupLoading = false; renderSetupWizard(); }
  }
}

async function updateSetupState(action, step = "") {
  if (state.user?.role !== "admin" || !state.token || state.setupLoading || state.setupSaving || !validSetupState(state.setupState)) return false;
  const requestId = ++state.setupRequestId;
  const authEpoch = state.authEpoch, token = state.token;
  const stillCurrent = () => requestId === state.setupRequestId && authEpoch === state.authEpoch
    && token === state.token && state.user?.role === "admin";
  const body = { expected_revision: state.setupState.revision, action, ...(action === "step" ? { step } : {}) };
  state.setupSaving = true;
  state.setupError = "";
  renderSetupWizard();
  try {
    const result = await api("/api/setup", { method: "PATCH", body });
    if (!stillCurrent()) return false;
    if (!validSetupState(result)) throw new Error("Setup response not confirmed");
    state.setupState = result;
    renderSetupWizard();
    if (result.status !== "pending") $("#setupDialog").close();
    else if (action === "reopen" && !$("#setupDialog").open) $("#setupDialog").showModal();
    return true;
  } catch (requestError) {
    if (!stillCurrent()) return false;
    if (requestError.status === 409) {
      state.setupSaving = false;
      const loaded = await loadSetupState();
      if (loaded && authEpoch === state.authEpoch && token === state.token) {
        state.setupError = "向导进度已在其他会话更新，已读取最新进度，请重试。";
        renderSetupWizard();
      }
      return false;
    }
    state.setupError = requestError.status === 403 ? "当前账号没有管理安装向导的权限。" : "向导进度暂时无法保存，请重试。";
    return false;
  } finally {
    if (stillCurrent()) { state.setupSaving = false; renderSetupWizard(); }
  }
}

async function openSetupWizard() {
  if (state.setupLoading || state.setupSaving || !await loadSetupState()) return;
  await updateSetupState("reopen");
}

function openSetupPage(page) {
  if (state.user?.role !== "admin" || state.setupLoading || state.setupSaving || !["wallets", "pools", "flights"].includes(page)) return;
  $("#setupDialog").close();
  showPage(page);
}

function setPasswordChangeBusy(busy) {
  $$("input, button", $("#passwordChangeForm")).forEach((element) => { element.disabled = Boolean(busy); });
  $("#passwordChangeForm").setAttribute("aria-busy", String(Boolean(busy)));
  $("#submitPasswordChangeButton").textContent = busy ? "正在修改…" : "修改密码";
}

function clearPasswordChangeForm() {
  state.passwordChangeRequest = null;
  for (const selector of ["#passwordChangeCurrent", "#passwordChangeNew", "#passwordChangeConfirm"]) $(selector).value = "";
  $("#passwordChangeError").textContent = "";
  setPasswordChangeBusy(false);
}

function openPasswordChange() {
  if (state.user?.role !== "admin" || !state.token) return;
  clearPasswordChangeForm();
  $("#passwordChangeDialog").showModal();
  $("#passwordChangeCurrent").focus();
}

async function changeOwnPassword(event) {
  event.preventDefault();
  const dialog = $("#passwordChangeDialog");
  if (state.user?.role !== "admin" || !state.token || !dialog.open || state.passwordChangeRequest) return;
  const error = $("#passwordChangeError");
  error.textContent = "";
  const currentPassword = $("#passwordChangeCurrent").value;
  const newPassword = $("#passwordChangeNew").value;
  const confirmPassword = $("#passwordChangeConfirm").value;
  const length = (value) => Array.from(value).length;
  if (length(currentPassword) < 1 || length(currentPassword) > 256) {
    error.textContent = "请填写当前密码，最多 256 字符。";
    return;
  }
  if (length(newPassword) < 12 || length(newPassword) > 256) {
    error.textContent = "新密码须为 12–256 字符。";
    return;
  }
  if (confirmPassword !== newPassword) {
    error.textContent = "两次新密码不一致，请重新确认。";
    return;
  }
  const username = state.user.username;
  const request = { authEpoch: state.authEpoch, token: state.token };
  const stillCurrent = () => state.passwordChangeRequest === request && state.authEpoch === request.authEpoch
    && state.token === request.token && dialog.open;
  state.passwordChangeRequest = request;
  setPasswordChangeBusy(true);
  try {
    const result = await api("/api/auth/password", { method: "POST", body: {
      current_password: currentPassword, new_password: newPassword, confirm_password: confirmPassword,
    } });
    if (!stillCurrent()) return;
    if (result?.changed !== true || result?.reauthenticate !== true) throw new Error("Password change response not confirmed");
    logout(false);
    $("#username").value = username;
    $("#loginError").textContent = "密码已修改，请使用新密码重新登录。";
    $("#password").focus();
  } catch (requestError) {
    if (!stillCurrent()) return;
    const messages = {
      400: "当前密码不正确，请修改后重试。",
      403: "当前账号没有修改密码的权限。",
      422: "请检查密码长度和两次新密码是否一致。",
      429: "尝试过于频繁，请稍后重试。",
    };
    error.textContent = Object.hasOwn(messages, requestError.status) ? messages[requestError.status]
      : "暂时无法确认修改结果，请重新登录核对。";
  } finally {
    if (stillCurrent()) {
      state.passwordChangeRequest = null;
      setPasswordChangeBusy(false);
    }
  }
}

function renderUsers() {
  const container = $("#userList");
  if (!container) return;
  container.innerHTML = "";
  if (state.user?.role !== "admin") return;
  const visibleUsers = state.users.filter((user) => user?.is_active !== false);
  container.innerHTML = visibleUsers.map((user) => {
    const isCurrent = user.username === state.user.username;
    return `<div class="user-row">
      <span class="avatar">${escapeHtml(user.username.slice(0, 1).toUpperCase())}</span>
      <div><strong>${escapeHtml(user.username)}${isCurrent ? "（当前账号）" : ""}</strong><small>可登录 · ${formatTime(user.created_at, false)}</small></div>
      <div class="user-row-actions"><span class="command-status">${roleLabel(user.role)}</span>${!isCurrent ? `<button class="text-button danger-text" type="button" data-delete-user="${escapeHtml(user.id)}" data-user-name="${escapeHtml(user.username)}">删除</button>` : ""}</div>
    </div>`;
  }).join("");
  $$('[data-delete-user]', container).forEach((button) => button.addEventListener("click", () => deleteUser(button.dataset.deleteUser, button.dataset.userName)));
}

async function deleteUser(userId, username) {
  if (state.user?.role !== "admin" || !userId) return;
  if (!window.confirm(`确认删除控制台用户“${username}”？该账号将立即无法登录。`)) return;
  try {
    await api(`/api/users/${encodeURIComponent(userId)}`, { method: "DELETE" });
    toast(`用户“${username}”已删除`);
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteOfflineAgent(agentId, agentName) {
  if (state.user?.role !== "admin" || !agentId) return;
  const displayName = agentName || "未命名设备";
  if (!window.confirm(`确认永久删除离线设备“${displayName}”？\n\n该设备的遥测与历史任务记录也会删除，且无法恢复。若仍有已送达或执行中的任务，将只结束控制端遗留记录；无法保证设备端动作尚未执行。`)) return;
  try {
    await api(`/api/agents/${encodeURIComponent(agentId)}?abandon_unfinished=true`, { method: "DELETE" });
    if (state.selectedAgent?.id === agentId) closeDrawer();
    toast(`离线设备“${displayName}”已删除`);
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function controlMining(action, agentId, agentName, button) {
  if (!(state.user?.role === "operator" || state.user?.role === "admin")) return;
  if (!["stop_mining", "restart_mining"].includes(action) || !agentId) return;
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent || agent.status !== "online") { toast("设备离线，无法控制矿工", "error"); return; }
  if (pendingMiningControlReason(agent)) { toast(pendingMiningControlReason(agent), "error"); return; }
  const displayName = agentName || "未命名设备";
  const question = action === "stop_mining"
    ? `确认停止设备“${displayName}”当前的挖矿进程？\n\n停止矿工后会尝试把设备上的 NVML OC 恢复为硬件默认值；控制端保存的 OC 配置不会删除。恢复失败时任务会明确标记失败。`
    : `确认重启设备“${displayName}”当前的挖矿进程？\n\n挖矿会短暂中断。`;
  if (!window.confirm(question)) return;
  button.disabled = true;
  try {
    await api(`/api/agents/${encodeURIComponent(agentId)}/commands`, {
      method: "POST",
      body: { action, payload: {} },
    });
    toast(`${actionNames[action]}任务已进入队列`);
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

function renderWallets() {
  const body = $("#walletTableBody");
  if (!body) return;
  const isAdmin = state.user?.role === "admin";
  const canEdit = state.user?.role === "operator" || isAdmin;
  body.innerHTML = state.wallets.map((wallet) => `<tr>
    <td><strong>${escapeHtml(wallet.name)}</strong><small class="table-subline">${formatTime(wallet.created_at, false)}</small></td>
    <td><span class="tag">${escapeHtml(wallet.coin)}</span></td>
    <td><code class="address-cell" title="${escapeHtml(wallet.address)}">${escapeHtml(wallet.address)}</code></td>
    <td>${escapeHtml(wallet.note || "—")}</td>
    <td>${escapeHtml(wallet.created_by)}</td>
    <td><div class="table-row-actions">${canEdit ? `<button class="text-button" type="button" data-edit-wallet="${escapeHtml(wallet.id)}">编辑</button>` : ""}${isAdmin ? `<button class="text-button danger-text" type="button" data-delete-wallet="${escapeHtml(wallet.id)}">删除</button>` : ""}</div></td>
  </tr>`).join("");
  $("#walletEmpty").classList.toggle("hidden", state.wallets.length > 0);
  setTableVisible(body.closest("table"), state.wallets.length > 0);
  $$('[data-edit-wallet]', body).forEach((button) => button.addEventListener("click", () => openWalletDialog(state.wallets.find((wallet) => wallet.id === button.dataset.editWallet))));
  $$('[data-delete-wallet]', body).forEach((button) => button.addEventListener("click", () => deleteResource("wallet", button.dataset.deleteWallet)));
}

function poolProtocolLabel(protocol) {
  return { "stratum+tcp": "TCP", "stratum+ssl": "SSL / TLS", "stratum+tls": "TLS", pearl: "PEARL", quic: "QUIC" }[protocol] || protocol || "—";
}

function renderPools() {
  const body = $("#poolTableBody");
  if (!body) return;
  const pools = Array.isArray(state.pools) ? state.pools : [];
  const canEdit = ["operator", "admin"].includes(state.user?.role);
  const isAdmin = state.user?.role === "admin";
  body.innerHTML = pools.map((pool) => `<tr><td><strong>${escapeHtml(pool.name)}</strong><small class="table-subline">${escapeHtml(formatTime(pool.updated_at, false))}</small></td><td><span class="tag">${escapeHtml(pool.coin)}</span></td><td><code class="address-cell" title="${escapeHtml(pool.pool_url)}">${escapeHtml(pool.pool_url)}</code></td><td>${escapeHtml(poolProtocolLabel(pool.protocol))}</td><td>${pool.pool_password_configured ? "已设置" : "未设置"}</td><td>${escapeHtml(pool.note || "—")}</td><td>${escapeHtml(pool.created_by)}</td><td><div class="table-row-actions">${canEdit ? `<button class="text-button" type="button" data-edit-pool="${escapeHtml(pool.id)}">编辑</button>` : ""}${isAdmin ? `<button class="text-button danger-text" type="button" data-delete-pool="${escapeHtml(pool.id)}">删除</button>` : ""}</div></td></tr>`).join("");
  $("#poolEmpty").classList.toggle("hidden", pools.length > 0);
  setTableVisible(body.closest("table"), pools.length > 0);
  $$('[data-edit-pool]', body).forEach((button) => button.addEventListener("click", () => openPoolDialog(pools.find((pool) => pool.id === button.dataset.editPool))));
  $$('[data-delete-pool]', body).forEach((button) => button.addEventListener("click", () => deleteResource("pool", button.dataset.deletePool)));
}

function openPoolDialog(pool = null) {
  if (!["operator", "admin"].includes(state.user?.role)) return;
  resetResourceEditor($("#savePoolButton"));
  $("#poolForm").reset();
  $("#poolError").textContent = "";
  state.editingPoolId = pool?.id || null;
  $("#poolDialogTitle").textContent = pool ? "编辑矿池" : "添加矿池";
  $("#savePoolButton").textContent = pool ? "保存修改" : "保存矿池";
  if (pool) {
    $("#poolName").value = pool.name || "";
    $("#poolCoin").value = pool.coin || "";
    $("#poolUrl").value = pool.pool_url || "";
    $("#poolPassword").value = pool.pool_password ?? "";
    $("#poolNote").value = pool.note || "";
  }
  $("#poolDialog").showModal();
}

async function savePool() {
  const button = $("#savePoolButton");
  const current = beginResourceSave(button);
  if (!current) return;
  const error = $("#poolError");
  error.textContent = "";
  try {
    const editing = state.editingPoolId;
    await api(editing ? `/api/pools/${encodeURIComponent(editing)}` : "/api/pools", {
      method: editing ? "PUT" : "POST",
      body: { name: $("#poolName").value.trim(), coin: $("#poolCoin").value.trim(),
        pool_url: $("#poolUrl").value.trim(), pool_password: $("#poolPassword").value,
        note: $("#poolNote").value.trim() },
    });
    if (!current()) return;
    $("#poolDialog").close();
    state.editingPoolId = null;
    toast("矿池记录已保存；已有飞行表不会自动改变或下发");
    await refreshAll(true);
  } catch (saveError) {
    if (current()) error.textContent = saveError.message;
  } finally {
    if (current()) button.disabled = false;
  }
}

function poolSelectorOptions(coin, selected = "") {
  const pools = (Array.isArray(state.pools) ? state.pools : []).filter((pool) => !coin || pool.coin === coin.toUpperCase());
  return { pools, html: '<option value="">手工填写矿池地址</option>' + pools.map((pool) => `<option value="${escapeHtml(pool.id)}">${escapeHtml(pool.coin)} · ${escapeHtml(pool.name)} · ${escapeHtml(poolProtocolLabel(pool.protocol))}</option>`).join(""), selected };
}

function populateSavedPoolSelectors() {
  const options = [
    ["#flightSavedPool", state.wallets.find((item) => item.id === $("#flightWallet")?.value)?.coin || ""],
    ["#flightSecondarySavedPool", state.wallets.find((item) => item.id === $("#flightSecondaryWallet")?.value)?.coin || ""],
  ];
  for (const [selector, coin] of options) {
    const select = $(selector);
    if (!select) continue;
    const { pools, html } = poolSelectorOptions(coin, select.value);
    const selected = select.value;
    select.innerHTML = html;
    select.value = pools.some((pool) => pool.id === selected) ? selected : "";
  }
}

function selectedSavedPool(selector, error) {
  const select = $(selector);
  const pool = state.pools.find((item) => item.id === select?.value);
  if (!pool) { if (select?.value) error.textContent = "所选矿池已不存在，请刷新后重试。"; return null; }
  if (typeof pool.pool_password !== "string") {
    error.textContent = "当前账号不能读取矿池密码，无法复制到飞行表。";
    select.value = "";
    return null;
  }
  return pool;
}

function applySavedPoolToFlight(prefix) {
  const secondary = prefix === "flightSecondary";
  const selector = secondary ? "#flightSecondarySavedPool" : "#flightSavedPool";
  const error = $("#flightError");
  error.textContent = "";
  if (!$(selector).value) return;
  const pool = selectedSavedPool(selector, error);
  if (!pool) return;
  const walletId = $(secondary ? "#flightSecondaryWallet" : "#flightWallet").value;
  const wallet = state.wallets.find((item) => item.id === walletId);
  if (!wallet || wallet.coin.toUpperCase() !== pool.coin) {
    error.textContent = `请先明确选择 ${pool.coin} 钱包；选择矿池不会自动更换钱包。`;
    $(selector).value = "";
    return;
  }
  const minerType = $(secondary ? "#flightSecondaryMinerType" : "#flightMinerType").value;
  if (pool.protocol === "pearl" && minerType !== "pearlminer") {
    error.textContent = "pearl:// 矿池地址仅适用于 Pearl Miner，请选择兼容软件或另一矿池。";
    $(selector).value = "";
    return;
  }
  $(secondary ? "#flightSecondaryPool" : "#flightPool").value = pool.pool_url;
  $(secondary ? "#flightSecondaryPoolPassword" : "#flightPoolPassword").value = pool.pool_password;
  toast(`已复制 ${pool.coin} 矿池到${secondary ? "第二矿工" : "主挖"}；保存前仍可核对原始地址与密码`);
}

const MINER_PACKAGE_TYPE_LABELS = Object.freeze({
  xmrig: "XMRig",
  srbminer: "SRBMiner-MULTI",
  pearlminer: "Pearl Miner",
});

// These names remain displayable on user history, but never re-enter a catalog.
const RETIRED_MINER_LABELS = Object.freeze({ lolminer: "lolMiner", forgeminer: "ForgeMiner", hiveos: "HiveOS" });

function minerPackageTypeLabel(minerType) {
  const normalized = String(minerType || "").trim().toLowerCase();
  return String(
    minerAdapterDefinition(normalized)?.display_name
    || (RETIRED_MINER_LABELS[normalized] ? `${RETIRED_MINER_LABELS[normalized]} · 已退休` : "")
    || MINER_PACKAGE_TYPE_LABELS[normalized]
    || "",
  ).trim();
}

function populateMinerPackageManagedTypeSelect(selectedType = "") {
  const select = $("#minerPackageManagedType");
  if (!select) return;
  const current = String(selectedType || select.value || "").trim().toLowerCase();
  const adapters = availableMinerAdapters();
  select.innerHTML = '<option value="">未归类（普通自定义包）</option>' + adapters.map((adapter) => {
    const minerType = String(adapter.miner_type || "").trim().toLowerCase();
    return `<option value="${escapeHtml(minerType)}" data-adapter-id="${escapeHtml(adapter.adapter_id || "")}">${escapeHtml(adapter.display_name || minerType)}</option>`;
  }).join("");
  if (current && !adapters.some((adapter) => String(adapter.miner_type || "").trim().toLowerCase() === current)) {
    select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(current)}" data-history-adapter="true">${escapeHtml(current)} · 历史适配器</option>`);
  }
  select.value = current;
}

function renderMinerPackages() {
  const body = $("#minerPackageTableBody");
  if (!body) return;
  const isAdmin = state.user?.role === "admin";
  rememberDisclosures(body);
  body.innerHTML = state.minerPackages.map((minerPackage) => {
    const sourceType = String(minerPackage.source_type || "external").toLowerCase();
    const sourceLabel = sourceType === "uploaded" ? "控制端托管" : sourceType === "imported" ? "控制端托管" : "外部地址（兼容）";
    const filename = minerPackage.original_filename || String(minerPackage.download_url || "").split("/").pop() || "—";
    const managedType = String(minerPackage.managed_miner_type || "").toLowerCase();
    const managedOs = String(minerPackage.managed_os_family || "").toLowerCase();
    const managedVariant = String(minerPackage.managed_variant || "").toLowerCase();
    const managedChannel = String(minerPackage.managed_channel || "").toLowerCase();
    const isTestPackage = ["test", "beta", "preview", "prerelease"].includes(managedChannel);
    const managedLabel = minerPackageTypeLabel(managedType);
    const classified = Boolean(managedLabel);
    const classification = classified
      ? [
        managedLabel,
        managedOs === "windows" ? "Windows" : (managedOs === "linux" ? "Linux" : ""),
        managedVariant ? managedVariant.toUpperCase() : "",
        managedChannel === "test" ? "测试版" : (managedChannel === "custom" ? "自定义包" : managedChannel),
      ].filter(Boolean).join(" · ")
      : "未归类";
    const displayName = classified ? managedLabel : minerPackage.name;
    return `<tr>
      <td><strong>${escapeHtml(displayName)}</strong>${isTestPackage ? '<span class="tag package-channel">测试版</span>' : ""}<small class="table-subline">版本 ${escapeHtml(minerPackage.version)}</small></td>
      <td><span class="tag">${escapeHtml(String(minerPackage.archive_type || "").toUpperCase())}</span></td>
      <td><span class="tag package-source ${escapeHtml(sourceType)}">${escapeHtml(sourceLabel)}</span><small class="table-subline">${escapeHtml(classification)}</small></td>
      <td><code class="address-cell package-url" title="${escapeHtml(filename)}">${escapeHtml(filename)}</code><small class="table-subline">${formatBytes(minerPackage.file_size)}</small></td>
      <td>${escapeHtml(minerPackage.note || "—")}</td>
      <td>${escapeHtml(minerPackage.created_by || "—")}</td>
      <td>${isAdmin ? `<button class="text-button danger-text" data-delete-miner-package="${escapeHtml(minerPackage.id)}">删除</button>` : ""}</td>
    </tr>`;
  }).join("");
  restoreDisclosures(body);
  $("#minerPackageEmpty").classList.toggle("hidden", state.minerPackages.length > 0);
  setTableVisible(body.closest("table"), state.minerPackages.length > 0);
  $$('[data-delete-miner-package]', body).forEach((button) => button.addEventListener("click", () => deleteResource("minerPackage", button.dataset.deleteMinerPackage)));
}

function agentReleaseTargetLabel(release) {
  const platformValue = String(release?.platform || "").toLowerCase();
  const platform = platformValue === "windows" ? "Windows" : platformValue === "linux" ? "Linux" : String(release?.platform || "未知平台");
  const architecture = ["x86_64", "x64", "amd64"].includes(String(release?.architecture || "").toLowerCase())
    ? "x64"
    : String(release?.architecture || "未知架构");
  return `${platform} ${architecture}`;
}

function agentReleaseDigestMarkup(value, releaseId = "") {
  const digest = String(value || "");
  if (!digest) return "—";
  const shortDigest = digest.length > 22 ? `${digest.slice(0, 12)}…${digest.slice(-8)}` : digest;
  return `<details class="release-digest"${releaseId ? ` data-disclosure-key="${escapeHtml(disclosureKey("agent-release-table", releaseId, "digest", digest))}"` : ""}><summary><code>${escapeHtml(shortDigest)}</code></summary><code class="release-digest-full">${escapeHtml(digest)}</code></details>`;
}

function agentVersionParts(value) {
  const match = String(value || "").match(/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/);
  return match ? match.slice(1).map(Number) : null;
}

function compareAgentVersions(left, right) {
  const leftParts = agentVersionParts(left);
  const rightParts = agentVersionParts(right);
  if (!leftParts || !rightParts) return null;
  for (let index = 0; index < 3; index += 1) {
    if (leftParts[index] !== rightParts[index]) return leftParts[index] - rightParts[index];
  }
  return 0;
}

function agentUpdateReadinessDetail(agent, platform) {
  const diagnostic = agent?.inventory?.agent_update_readiness;
  const reason = diagnostic?.schema_version === 1
    && diagnostic?.protocol === "agent_update.v1"
    && diagnostic?.available === false
    ? String(diagnostic.reason || "")
    : "";
  const windowsReasons = {
    config_disabled: "本机配置已关闭 allow_agent_updates；请确认本地策略后再启用",
    marker_missing: "更新就绪标记缺失；请以管理员身份重新运行 Install-Autostart.bat",
    marker_invalid: "更新就绪标记与受信更新器不匹配；请以管理员身份重新运行 Install-Autostart.bat",
    root_mismatch: "Agent 未从受保护的 ProgramData 固定目录运行；请重新运行 Install-Autostart.bat 完成迁移",
    acl_or_owner_mismatch: "受保护安装目录的 ACL 或所有者不安全；请以管理员身份重新运行 Install-Autostart.bat 修复",
    helper_missing: "本地更新辅助组件缺失；请用最新版完整包覆盖后重新运行 Install-Autostart.bat",
    state_mismatch: "设备身份数据不在受保护数据目录；请重新运行 Install-Autostart.bat 修复安装",
    active_version_mismatch: "当前版本槽指针无效；请用最新版完整包修复安装",
    unsupported_runtime: "当前 Agent 运行方式不支持在线更新；请安装 Windows 便携完整包",
    unsafe_layout: "本地更新布局未通过安全检查；请以管理员身份重新运行 Install-Autostart.bat",
  };
  if (platform === "windows") {
    return windowsReasons[reason]
      || "未报告 agent_update.v1；请在设备检查受保护安装目录、allow_agent_updates，并以管理员身份重新运行 Install-Autostart.bat";
  }
  const linuxReasons = {
    config_disabled: "本机配置已关闭 allow_agent_updates；请确认本地策略后再启用",
    root_mismatch: "Agent 未从受保护的 systemd 固定目录运行；请安装最新版 Ubuntu 完整包一次",
    acl_or_owner_mismatch: "Linux 安装目录的所有者或权限不安全；请安装最新版 Ubuntu 完整包一次",
    helper_missing: "Linux 在线更新辅助组件缺失；请安装最新版 Ubuntu 完整包一次",
    state_mismatch: "设备身份数据不在受保护数据目录；请安装最新版 Ubuntu 完整包一次",
    active_version_mismatch: "当前 Linux 版本槽指针无效；请安装最新版 Ubuntu 完整包一次",
    unsupported_runtime: "当前 Agent 不是 root systemd 固定安装；请安装最新版 Ubuntu 完整包一次",
    unsafe_layout: "Linux 在线更新布局未通过安全检查；请安装最新版 Ubuntu 完整包一次",
  };
  return linuxReasons[reason]
    || "未报告 agent_update.v1；请在设备手动安装最新版 Ubuntu 完整包一次";
}

function agentUpdateEligibility(agent, release = null) {
  if (!agent) return { eligible: false, reason: "设备不存在" };
  if (deviceRuntimePreparationReason(agent)) return { eligible: false, reason: deviceRuntimePreparationReason(agent) };
  if (agent.status !== "online") return { eligible: false, reason: "设备不在线" };
  const osValue = String(agent.os_family || "").toLowerCase();
  const platform = ["windows", "win", "win32"].includes(osValue)
    ? "windows"
    : ["linux", "ubuntu", "debian"].includes(osValue) ? "linux" : "";
  if (!platform) {
    return { eligible: false, reason: "仅支持 Windows 或 Linux" };
  }
  const architecture = String(agent.inventory?.architecture || "").toLowerCase().replaceAll("-", "_");
  if (!["amd64", "x86_64", "x64"].includes(architecture)) {
    return { eligible: false, reason: "设备未报告 x86_64 架构" };
  }
  const linuxBridgeComparison = compareAgentVersions(agent.agent_version, "0.6.7");
  if (platform === "linux" && linuxBridgeComparison !== null && linuxBridgeComparison < 0) {
    return { eligible: false, reason: "Linux Agent 0.6.6 或更早版本可能仍含旧更新桥；请手动安装 0.6.8 完整包一次" };
  }
  if (!Array.isArray(agent.capabilities) || !agent.capabilities.includes("agent_update.v1")) {
    return { eligible: false, reason: agentUpdateReadinessDetail(agent, platform) };
  }
  if (["requesting", "queued"].includes(state.agentUpdateRequests[agent.id]?.status)) {
    return { eligible: false, reason: "此设备的更新请求正在处理" };
  }
  if (state.agentUpdates.some((update) => update.agent_id === agent.id && ["queued", "dispatched", "activating"].includes(update.status))) {
    return { eligible: false, reason: "已有被控端更新正在进行" };
  }
  if (state.commands.some((command) => command.agent_id === agent.id && ["queued", "dispatched", "running"].includes(command.status))) {
    return { eligible: false, reason: "设备还有其他远程任务未完成" };
  }
  if (release) {
    if (release.deployable !== true || release.update_protocol !== "agent_update.v1") {
      return { eligible: false, reason: "旧版存档包仅可下载，不能远程下发" };
    }
    if (String(release.platform || "").toLowerCase() !== platform) {
      return { eligible: false, reason: `平台不匹配：设备为 ${platform === "windows" ? "Windows" : "Linux"}` };
    }
    if (!agentReleaseMatchesDevice(agent, release)) {
      return { eligible: false, reason: "平台或架构与更新包不匹配" };
    }
    const comparison = compareAgentVersions(release.version, agent.agent_version);
    if (comparison === 0) return { eligible: false, reason: "设备已是此版本" };
    if (comparison === null) return { eligible: false, reason: "无法比较版本号" };
    if (comparison < 0) return { eligible: false, reason: "禁止远程降级" };
  }
  return { eligible: true, reason: "兼容，可下发" };
}

const agentUpdateStatusLabels = {
  queued: "等待设备领取",
  dispatched: "已下发",
  activating: "正在替换并回连",
  succeeded: "更新成功",
  failed: "更新失败",
  cancelled: "已取消",
  expired: "等待回连超时",
};

function agentReleaseNotesLabel(notes) {
  const value = typeof notes === "string" ? notes.trim() : "";
  return value ? (historicalAgentReleaseNotes[value] || value) : "—";
}

function renderAgentReleases() {
  const body = $("#agentReleaseTableBody");
  if (!body) return;
  const isAdmin = state.user?.role === "admin";
  const platformFilter = $("#agentReleasePlatformFilter")?.value || "all";
  const releases = state.agentReleases.filter((release) => platformFilter === "all" || release.platform === platformFilter);
  rememberDisclosures(body);
  body.innerHTML = releases.map((release) => {
    const hasHistory = state.agentUpdates.some((update) => update.release_id === release.id);
    return `<tr>
    <td><strong>RigPilot Agent ${escapeHtml(release.version)}</strong><small class="table-subline">${release.deployable ? "支持 agent_update.v1" : "旧存档，仅下载"}</small></td>
    <td><span class="tag release-platform">${escapeHtml(agentReleaseTargetLabel(release))}</span><small class="table-subline">${release.platform === "linux" ? "TAR.GZ" : "ZIP"}</small></td>
    <td><code class="address-cell release-file" title="${escapeHtml(release.file_name)}">${escapeHtml(release.file_name)}</code><small class="table-subline">${formatBytes(release.size)}</small></td>
    <td>${agentReleaseDigestMarkup(release.sha256, release.id)}</td>
    <td class="release-notes">${escapeHtml(agentReleaseNotesLabel(release.notes))}</td>
    <td><strong>${escapeHtml(release.created_by || "—")}</strong><small class="table-subline">${escapeHtml(formatTime(release.created_at, false))}</small></td>
    <td><div class="table-row-actions"><button class="text-button update-release-button" type="button" data-deploy-agent-release="${escapeHtml(release.id)}" ${release.deployable ? "" : 'disabled title="旧包没有更新清单，只能下载"'}>${release.deployable ? "指定设备下发" : "不可下发"}</button><button class="text-button" type="button" data-download-agent-release="${escapeHtml(release.id)}">下载</button>${isAdmin ? `<button class="text-button danger-text" type="button" data-delete-agent-release="${escapeHtml(release.id)}" data-agent-release-version="${escapeHtml(release.version)}" ${hasHistory ? 'disabled title="已有更新历史，不能删除"' : ""}>删除</button>` : ""}</div></td>
  </tr>`;
  }).join("");
  restoreDisclosures(body);
  $("#agentReleaseEmpty").classList.toggle("hidden", releases.length > 0);
  setTableVisible(body.closest("table"), releases.length > 0);
  $$('[data-download-agent-release]', body).forEach((button) => button.addEventListener("click", () => downloadAgentRelease(button.dataset.downloadAgentRelease, button)));
  $$('[data-delete-agent-release]', body).forEach((button) => button.addEventListener("click", () => deleteAgentRelease(button.dataset.deleteAgentRelease, button.dataset.agentReleaseVersion)));
  $$('[data-deploy-agent-release]', body).forEach((button) => button.addEventListener("click", () => openAgentUpdateDialog(button.dataset.deployAgentRelease)));
}

function agentUpdateRetryTarget(update) {
  // A failed rollout is safely retryable when the device is back online on
  // the old version and a newer compatible release exists right now.  This
  // mirrors the dispatch dialog's own eligibility instead of a second rule.
  if (state.user?.role !== "admin" || !["failed", "expired"].includes(update.status)) return null;
  const agent = state.agents.find((item) => item.id === update.agent_id);
  if (!agent) return null;
  const release = latestCompatibleAgentRelease(agent);
  if (!release) return null;
  return agentUpdateEligibility(agent, release).eligible ? {agent, release} : null;
}

function renderAgentUpdates() {
  const body = $("#agentUpdateTableBody");
  if (!body) return;
  body.innerHTML = state.agentUpdates.map((update) => {
    const canCancel = update.status === "queued";
    const stalled = update.status === "activating" && Boolean(update.stalled_at);
    const statusClass = update.status === "activating" ? (stalled ? "failed" : "running") : update.status;
    const statusText = update.status === "activating" && stalled
      ? "正在替换并回连 · 疑似卡滞"
      : agentUpdateStatusLabels[update.status] || update.status;
    const progress = update.status === "activating"
      ? stalled
        ? "新版已回连过但疑似卡滞（已回滚或未运行）；等待回滚上报或超时清算"
        : `目标版本稳定心跳 ${Math.min(Number(update.target_heartbeat_count) || 0, 2)}/2`
      : update.downloaded_at ? "包已请求" : "尚未下载";
    const retry = agentUpdateRetryTarget(update);
    return `<tr>
      <td><strong>${escapeHtml(update.agent_name)}</strong><small class="table-subline update-agent-id">${escapeHtml(update.agent_id)}</small></td>
      <td><strong>${escapeHtml(update.source_version)} → ${escapeHtml(update.target_version)}</strong><small class="table-subline">目标 Agent</small></td>
      <td><span class="command-status ${escapeHtml(statusClass)}">${escapeHtml(statusText)}</span><small class="table-subline">${escapeHtml(update.error || progress)}</small></td>
      <td><code class="address-cell release-file" title="${escapeHtml(update.file_name)}">${escapeHtml(update.file_name)}</code><small class="table-subline">${formatBytes(update.size)}</small></td>
      <td><strong>${escapeHtml(update.created_by || "—")}</strong><small class="table-subline">${escapeHtml(formatTime(update.created_at, false))}</small></td>
      <td>${canCancel ? `<button class="text-button danger-text" type="button" data-cancel-agent-update="${escapeHtml(update.command_id)}">取消</button>` : ""}${retry ? `<button class="text-button" type="button" data-agent-update-release="${escapeHtml(retry.release.id)}" data-agent-update-agent="${escapeHtml(retry.agent.id)}" title="重新下发更新（目标 Agent ${escapeHtml(retry.release.version)}）">重试更新</button>` : ""}</td>
    </tr>`;
  }).join("");
  $("#agentUpdateEmpty").classList.toggle("hidden", state.agentUpdates.length > 0);
  setTableVisible(body.closest("table"), state.agentUpdates.length > 0);
  $$('[data-cancel-agent-update]', body).forEach((button) => button.addEventListener("click", () => cancelAgentUpdate(button.dataset.cancelAgentUpdate)));
  $$('[data-agent-update-release]', body).forEach((button) => button.addEventListener("click", () => {
    openAgentUpdateDialog(button.dataset.agentUpdateRelease, button.dataset.agentUpdateAgent);
  }));
}

function openAgentUpdateDialog(releaseId, preselectedAgentId = "") {
  if (state.user?.role !== "admin") return;
  const release = state.agentReleases.find((item) => item.id === releaseId);
  if (!release) return;
  const form = $("#agentUpdateForm");
  form.reset();
  form.dataset.releaseId = release.id;
  $("#agentUpdateError").textContent = "";
  $("#agentUpdateReleaseSummary").innerHTML = `<strong>RigPilot Agent ${escapeHtml(release.version)}</strong><span>${escapeHtml(agentReleaseTargetLabel(release))} · ${escapeHtml(release.file_name)} · ${escapeHtml(formatBytes(release.size))}</span><code>SHA-256 ${escapeHtml(release.sha256)}</code>`;
  const selector = $("#agentUpdateAgent");
  const sortedAgents = [...state.agents].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  selector.innerHTML = '<option value="">请选择一台设备</option>' + sortedAgents.map((agent) => {
    const eligibility = agentUpdateEligibility(agent, release);
    const label = `${agent.name} · ${agent.id} · ${agent.agent_version || "未知版本"} · ${eligibility.reason}`;
    const selected = agent.id === preselectedAgentId ? "selected" : "";
    return `<option value="${escapeHtml(agent.id)}" ${eligibility.eligible ? "" : "disabled"} ${selected}>${escapeHtml(label)}</option>`;
  }).join("");
  if (preselectedAgentId && sortedAgents.some((agent) => agent.id === preselectedAgentId)) {
    syncAgentUpdateSelection();
  } else {
    $("#agentUpdateEligibilityDetail").textContent = sortedAgents.some((agent) => agentUpdateEligibility(agent, release).eligible)
      ? "请选择目标设备并再次核对设备 ID。"
      : "当前没有可远程更新的设备；未报告 agent_update.v1 的设备必须先手工引导一次。";
  }
  $("#agentUpdateDialog").showModal();
}

function syncAgentUpdateSelection() {
  const release = state.agentReleases.find((item) => item.id === $("#agentUpdateForm").dataset.releaseId);
  const agent = state.agents.find((item) => item.id === $("#agentUpdateAgent").value);
  const detail = $("#agentUpdateEligibilityDetail");
  if (!agent) {
    detail.textContent = "请选择目标设备。";
    return;
  }
  const eligibility = agentUpdateEligibility(agent, release);
  detail.textContent = `${agent.name} · ${agent.id} · 当前 ${agent.agent_version || "未知"} → 目标 ${release?.version || "未知"}：${eligibility.reason}`;
  detail.classList.toggle("is-error", !eligibility.eligible);
}

async function deployAgentRelease() {
  if (state.user?.role !== "admin") return;
  const form = $("#agentUpdateForm");
  const release = state.agentReleases.find((item) => item.id === form.dataset.releaseId);
  const agent = state.agents.find((item) => item.id === $("#agentUpdateAgent").value);
  const errorElement = $("#agentUpdateError");
  errorElement.textContent = "";
  if (!release || !agent) {
    errorElement.textContent = "请选择目标设备。";
    return;
  }
  const eligibility = agentUpdateEligibility(agent, release);
  if (!eligibility.eligible) {
    errorElement.textContent = eligibility.reason;
    return;
  }
  if (!$("#agentUpdateConfirm").checked) {
    errorElement.textContent = "请确认只向选中的这一台设备下发。";
    return;
  }
  const button = $("#deployAgentReleaseButton");
  const buttonLabel = button.textContent;
  const authEpoch = state.authEpoch;
  const requestId = ++state.agentUpdateRequestSequence;
  const pending = {
    status: "requesting", targetVersion: release.version, releaseId: release.id,
    commandId: null, serverCreatedAt: null, startedAt: Date.now(), authEpoch, requestId, error: "",
  };
  state.agentUpdateRequests[agent.id] = pending;
  renderDevices();
  button.disabled = true;
  button.textContent = "正在提交更新请求…";
  button.dataset.updateRequestId = String(requestId);
  const stillCurrent = () => state.authEpoch === authEpoch
    && state.user?.role === "admin"
    && state.agentUpdateRequests[agent.id] === pending;
  try {
    const queued = await api(`/api/agent-releases/${encodeURIComponent(release.id)}/deploy`, {
      method: "POST",
      body: { agent_id: agent.id },
    });
    if (!stillCurrent()) return;
    if (queued.agent_id !== agent.id || queued.release_id !== release.id
      || queued.target_version !== release.version || queued.status !== "queued"
      || !queued.command_id || !queued.created_at) throw new Error("控制端返回的更新任务与所选设备或版本不匹配");
    pending.status = "queued";
    pending.commandId = queued.command_id;
    pending.serverCreatedAt = String(queued.created_at);
    pending.startedAt = Date.now();
    renderDevices();
    $("#agentUpdateDialog").close();
    toast(`已为 ${agent.name}（${agent.id}）排队更新 Agent ${release.version}`);
    await refreshAll(true);
  } catch (error) {
    if (!stillCurrent()) return;
    pending.status = "request_failed";
    pending.error = error.message;
    pending.startedAt = Date.now();
    renderDevices();
    if ($("#agentUpdateDialog").open) errorElement.textContent = error.message;
  } finally {
    if (button.dataset.updateRequestId === String(requestId)) {
      button.disabled = false;
      button.textContent = buttonLabel;
      delete button.dataset.updateRequestId;
    }
  }
}

function openAgentBatchUpdateDialog() {
  if (state.user?.role !== "admin") return;
  const form = $("#agentBatchUpdateForm");
  form.reset();
  $("#agentBatchUpdateError").textContent = "";
  $("#agentBatchUpdateResults").innerHTML = "";
  $("#agentBatchUpdateResults").classList.add("hidden");
  const releases = state.agentReleases
    .filter((release) => release.deployable === true && release.update_protocol === "agent_update.v1")
    .sort((left, right) => String(right.created_at || "").localeCompare(String(left.created_at || "")));
  $("#agentBatchUpdateRelease").innerHTML = '<option value="">请选择发布包</option>' + releases.map((release) =>
    `<option value="${escapeHtml(release.id)}">RigPilot Agent ${escapeHtml(release.version)} · ${escapeHtml(agentReleaseTargetLabel(release))} · ${escapeHtml(release.file_name)}</option>`
  ).join("");
  $("#agentBatchUpdateAgents").innerHTML = releases.length
    ? '<div class="batch-choice-empty">选择发布包后才会列出设备，且默认一台也不选。</div>'
    : '<div class="batch-choice-empty">当前没有支持 agent_update.v1 的可下发发布包。</div>';
  $("#agentBatchUpdateEligibilityDetail").textContent = releases.length ? "请先选择发布包。" : "请先上传可远程下发的被控端包。";
  $("#agentBatchUpdateSelectedCount").textContent = "已选择 0 台";
  $("#deployAgentReleaseBatchButton").disabled = true;
  $("#agentBatchUpdateDialog").showModal();
}

function renderAgentBatchUpdateAgents() {
  const release = state.agentReleases.find((item) => item.id === $("#agentBatchUpdateRelease").value);
  const container = $("#agentBatchUpdateAgents");
  $("#agentBatchUpdateError").textContent = "";
  $("#agentBatchUpdateResults").innerHTML = "";
  $("#agentBatchUpdateResults").classList.add("hidden");
  if (!release) {
    container.innerHTML = '<div class="batch-choice-empty">选择发布包后才会列出设备，且默认一台也不选。</div>';
    $("#agentBatchUpdateEligibilityDetail").textContent = "请先选择发布包。";
    syncAgentBatchUpdateSelection();
    return;
  }
  const agents = [...state.agents].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  container.innerHTML = agents.length ? agents.map((agent) => {
    const eligibility = agentUpdateEligibility(agent, release);
    return `<label class="batch-choice${eligibility.eligible ? "" : " is-disabled"}">
      <input type="checkbox" data-agent-batch-update="${escapeHtml(agent.id)}"${eligibility.eligible ? "" : " disabled"}>
      <span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.id)} · 当前 ${escapeHtml(agent.agent_version || "未知")} → ${escapeHtml(release.version)} · ${escapeHtml(eligibility.reason)}</small></span>
    </label>`;
  }).join("") : '<div class="batch-choice-empty">当前没有已接入设备。</div>';
  $$("[data-agent-batch-update]", container).forEach((input) => input.addEventListener("change", syncAgentBatchUpdateSelection));
  const eligibleCount = agents.filter((agent) => agentUpdateEligibility(agent, release).eligible).length;
  $("#agentBatchUpdateEligibilityDetail").textContent = eligibleCount
    ? `前端预筛选：${eligibleCount} 台设备当前可选；最终结果以控制端逐台校验为准。`
    : "当前没有通过前端预筛选的设备；未报告能力或版本过旧的设备仍保留单机兼容提示。";
  syncAgentBatchUpdateSelection();
}

function selectedAgentBatchUpdateItems() {
  return $$("[data-agent-batch-update]:checked", $("#agentBatchUpdateAgents"))
    .map((input) => state.agents.find((agent) => agent.id === input.dataset.agentBatchUpdate))
    .filter(Boolean);
}

function syncAgentBatchUpdateSelection() {
  const selected = selectedAgentBatchUpdateItems();
  $("#agentBatchUpdateSelectedCount").textContent = `已选择 ${selected.length} 台`;
  $("#deployAgentReleaseBatchButton").disabled = !$("#agentBatchUpdateRelease").value || selected.length === 0;
}

function batchResponseEntries(response, key) {
  return Array.isArray(response?.[key]) ? response[key] : [];
}

function batchResponseMatch(entries, id, idKeys) {
  return entries.find((entry) => {
    if (typeof entry === "string") return entry === id;
    if (!entry || typeof entry !== "object") return false;
    return idKeys.some((key) => String(entry[key] || "") === id);
  });
}

function renderBatchOperationResults(target, selected, response, idKeys) {
  const results = batchResponseEntries(response, "results");
  const accepted = batchResponseEntries(response, "accepted");
  const rejected = batchResponseEntries(response, "rejected");
  target.innerHTML = selected.map((item) => {
    const direct = batchResponseMatch(results, item.id, idKeys);
    const acceptedEntry = batchResponseMatch(accepted, item.id, idKeys);
    const rejectedEntry = batchResponseMatch(rejected, item.id, idKeys);
    const entry = direct || rejectedEntry || acceptedEntry;
    const acceptedValue = direct && typeof direct.accepted === "boolean"
      ? direct.accepted
      : rejectedEntry ? false : Boolean(acceptedEntry || direct);
    const status = String(entry?.status || "");
    const successful = acceptedValue || ["accepted", "queued", "dispatched", "succeeded"].includes(status);
    const detail = typeof entry === "string"
      ? (successful ? "已接受" : "已拒绝")
      : String(entry?.reason || entry?.detail || entry?.message || entry?.error || status || (successful ? "已接受并进入队列" : "控制端未返回逐项原因"));
    return `<div class="batch-result ${successful ? "accepted" : "rejected"}"><span>${successful ? "✓" : "×"}</span><div><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.detail)}</small><p>${escapeHtml(detail)}</p></div></div>`;
  }).join("");
  target.classList.remove("hidden");
}

async function deployAgentReleaseBatch() {
  if (state.user?.role !== "admin") return;
  const release = state.agentReleases.find((item) => item.id === $("#agentBatchUpdateRelease").value);
  const selected = selectedAgentBatchUpdateItems();
  const errorElement = $("#agentBatchUpdateError");
  errorElement.textContent = "";
  if (!release) {
    errorElement.textContent = "请先选择发布包。";
    return;
  }
  if (!selected.length) {
    errorElement.textContent = "请明确勾选至少一台设备；系统不会默认全选。";
    return;
  }
  const names = selected.map((agent) => `${agent.name}（${agent.id}）`).join("\n");
  if (!window.confirm(`确认将 RigPilot Agent ${release.version} 下发到以下 ${selected.length} 台设备？\n\n${names}\n\n这些设备会分别停矿并进入更新流程；未勾选设备不受影响。`)) return;
  const button = $("#deployAgentReleaseBatchButton");
  const buttonLabel = button.textContent;
  const authEpoch = state.authEpoch;
  const requestGroup = ++state.agentUpdateRequestSequence;
  const pendingById = new Map(selected.map((agent) => {
    const pending = { status: "requesting", targetVersion: release.version, releaseId: release.id,
      commandId: null, serverCreatedAt: null, startedAt: Date.now(), authEpoch, requestId: requestGroup, error: "" };
    state.agentUpdateRequests[agent.id] = pending;
    return [agent.id, pending];
  }));
  renderDevices();
  button.disabled = true;
  button.textContent = "正在逐台提交…";
  button.dataset.updateRequestId = String(requestGroup);
  const stillCurrent = () => state.authEpoch === authEpoch && state.user?.role === "admin";
  try {
    const response = await api(`/api/agent-releases/${encodeURIComponent(release.id)}/deploy-batch`, {
      method: "POST",
      body: { agent_ids: selected.map((agent) => agent.id) },
    });
    if (!stillCurrent()) return;
    const accepted = batchResponseEntries(response, "accepted");
    const rejected = batchResponseEntries(response, "rejected");
    for (const agent of selected) {
      const pending = pendingById.get(agent.id);
      if (state.agentUpdateRequests[agent.id] !== pending) continue;
      const acceptedEntry = batchResponseMatch(accepted, agent.id, ["agent_id", "id"]);
      const rejectedEntry = batchResponseMatch(rejected, agent.id, ["agent_id", "id"]);
      if (acceptedEntry?.accepted === true && acceptedEntry.agent_id === agent.id
        && acceptedEntry.release_id === release.id && acceptedEntry.target_version === release.version
        && acceptedEntry.status === "queued" && acceptedEntry.command_id && acceptedEntry.created_at) {
        pending.status = "queued";
        pending.commandId = acceptedEntry.command_id;
        pending.serverCreatedAt = String(acceptedEntry.created_at);
      } else {
        pending.status = "request_failed";
        pending.error = String(rejectedEntry?.detail || "控制端未返回匹配的逐台更新任务");
      }
      pending.startedAt = Date.now();
    }
    renderDevices();
    renderBatchOperationResults(
      $("#agentBatchUpdateResults"),
      selected.map((agent) => ({ id: agent.id, label: agent.name, detail: agent.id })),
      response,
      ["agent_id", "id"],
    );
    toast(`批量更新请求已提交，控制端已逐台返回 ${selected.length} 项结果`);
    await refreshAll(true);
  } catch (error) {
    if (!stillCurrent()) return;
    for (const agent of selected) {
      const pending = pendingById.get(agent.id);
      if (state.agentUpdateRequests[agent.id] !== pending) continue;
      pending.status = "request_failed";
      pending.error = error.message;
      pending.startedAt = Date.now();
    }
    renderDevices();
    errorElement.textContent = error.message;
  } finally {
    if (button.dataset.updateRequestId === String(requestGroup)) {
      button.disabled = false;
      button.textContent = buttonLabel;
      delete button.dataset.updateRequestId;
    }
  }
}

function clearFlightBatchResults() {
  state.flightBatchView = {};
  $("#flightBatchApplyError").textContent = "";
  $("#flightBatchApplyResults").innerHTML = "";
  $("#flightBatchApplyResults").classList.add("hidden");
}

function flightBatchSubmitPending() {
  return Boolean(state.flightBatchRequest && state.flightBatchRequest.authEpoch === state.authEpoch);
}

function openFlightBatchApplyDialog() {
  if (!["admin", "operator"].includes(state.user?.role)) return;
  const form = $("#flightBatchApplyForm");
  form.reset();
  clearFlightBatchResults();
  $("#flightBatchApplySheet").innerHTML = '<option value="">请选择飞行表</option>'
    + state.flightSheets.map((sheet) => {
      const issue = flightSheetVersionIssue(sheet);
      const secondary = sheet.secondary?.coin ? ` + ${sheet.secondary.coin}` : "";
      return `<option value="${escapeHtml(sheet.id)}"${issue ? " disabled" : ""}>`
        + `${escapeHtml(sheet.name)} · ${escapeHtml(sheet.coin)}${escapeHtml(secondary)}`
        + `${issue ? " · 当前不可下发" : ""}</option>`;
    }).join("");
  $("#flightBatchApplyAgents").innerHTML = state.flightSheets.length
    ? '<div class="batch-choice-empty">选择飞行表后才会列出设备，且默认一台也不选。</div>'
    : '<div class="batch-choice-empty">当前还没有可用的飞行表。</div>';
  $("#flightBatchApplySelectedCount").textContent = "已选择 0 台";
  $("#applyFlightSheetBatchButton").disabled = true;
  $("#flightBatchApplyDialog").showModal();
}

function renderFlightBatchApplyAgents() {
  const sheet = state.flightSheets.find((item) => item.id === $("#flightBatchApplySheet").value);
  const container = $("#flightBatchApplyAgents");
  clearFlightBatchResults();
  if (!sheet) {
    container.innerHTML = '<div class="batch-choice-empty">选择飞行表后才会列出设备，且默认一台也不选。</div>';
    syncFlightBatchApplySelection();
    return;
  }
  const agents = [...filteredAgents()].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  container.innerHTML = agents.length ? agents.map((agent) => {
    const os = osLabel(agent);
    const eligibility = flightTargetEligibility(agent, sheet);
    return `<label class="batch-choice">
      <input type="checkbox" data-flight-batch-apply="${escapeHtml(agent.id)}"${eligibility.eligible ? "" : " disabled"}>
      <span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.id)} · ${escapeHtml(statusLabel(agent.status))} · ${escapeHtml(os.short)} · Agent ${escapeHtml(agent.agent_version || "未知")}${eligibility.reason || eligibility.warning ? ` · ${escapeHtml(eligibility.reason || eligibility.warning)}` : ""}</small></span>
    </label>`;
  }).join("") : '<div class="batch-choice-empty">当前搜索与状态筛选下没有设备。</div>';
  $$("[data-flight-batch-apply]", container).forEach((input) => input.addEventListener("change", () => {
    clearFlightBatchResults();
    syncFlightBatchApplySelection();
  }));
  syncFlightBatchApplySelection();
}

function selectedFlightBatchApplyItems() {
  return $$("[data-flight-batch-apply]:checked", $("#flightBatchApplyAgents"))
    .map((input) => state.agents.find((agent) => agent.id === input.dataset.flightBatchApply))
    .filter(Boolean);
}

function syncFlightBatchApplySelection() {
  const selected = selectedFlightBatchApplyItems();
  $("#flightBatchApplySelectedCount").textContent = `已选择 ${selected.length} 台`;
  const sheet = state.flightSheets.find((item) => item.id === $("#flightBatchApplySheet").value);
  const blocked = selected.map((agent) => ({ agent, eligibility: flightTargetEligibility(agent, sheet) })).find((item) => !item.eligibility.eligible);
  $("#applyFlightSheetBatchButton").disabled = flightBatchSubmitPending() || !sheet || selected.length === 0 || Boolean(blocked);
  $("#flightBatchApplyEligibilityHint").textContent = blocked
    ? `${blocked.agent.name}：${blocked.eligibility.reason}；请修改目标选择后再下发。`
    : "";
}

async function applyFlightSheetBatch() {
  if (!["admin", "operator"].includes(state.user?.role)) return;
  if (flightBatchSubmitPending()) return;
  const sheet = state.flightSheets.find((item) => item.id === $("#flightBatchApplySheet").value);
  const selected = selectedFlightBatchApplyItems();
  const errorElement = $("#flightBatchApplyError");
  errorElement.textContent = "";
  if (!sheet) {
    errorElement.textContent = "请先选择飞行表。";
    return;
  }
  const versionIssue = flightSheetVersionIssue(sheet);
  if (versionIssue) {
    errorElement.textContent = `${versionIssue}。请先编辑飞行表并选择当前可用版本。`;
    return;
  }
  if (!selected.length) {
    errorElement.textContent = "请明确勾选至少一台设备；系统不会默认全选。";
    return;
  }
  const blocked = selected.map((agent) => ({ agent, eligibility: flightTargetEligibility(agent, sheet) })).find((item) => !item.eligibility.eligible);
  if (blocked) { errorElement.textContent = `${blocked.agent.name}：${blocked.eligibility.reason}`; syncFlightBatchApplySelection(); return; }
  const names = selected.map((agent) => `${agent.name}（${agent.id}）`).join("\n");
  if (!window.confirm(`确认将飞行表“${sheet.name}”应用到以下 ${selected.length} 台设备？\n\n${names}\n\n控制端会逐台完整校验；未勾选设备不受影响。`)) return;
  const button = $("#applyFlightSheetBatchButton");
  const request = { authEpoch: state.authEpoch, view: state.flightBatchView };
  state.flightBatchRequest = request;
  const current = () => request.authEpoch === state.authEpoch && request.view === state.flightBatchView
    && $("#flightBatchApplyDialog").open;
  button.disabled = true;
  try {
    const response = await api(`/api/flight-sheets/${encodeURIComponent(sheet.id)}/apply-batch`, {
      method: "POST",
      body: { agent_ids: selected.map((agent) => agent.id) },
    });
    if (current()) {
      renderBatchOperationResults(
        $("#flightBatchApplyResults"),
        selected.map((agent) => ({ id: agent.id, label: agent.name, detail: agent.id })),
        response,
        ["agent_id", "id"],
      );
      toast(`批量应用已逐台处理：${response.accepted_count || 0} 台进入队列，${response.rejected_count || 0} 台被拒绝`);
    }
    if (request.authEpoch === state.authEpoch) await refreshAll(true);
  } catch (error) {
    if (current()) errorElement.textContent = error.message;
  } finally {
    if (state.flightBatchRequest === request) {
      state.flightBatchRequest = null;
      if (request.authEpoch === state.authEpoch) syncFlightBatchApplySelection();
    }
  }
}
function miningControlBatchEligibility(agent, action) {
  if (!agent) return { eligible: false, reason: "设备不存在" };
  if (agent.status !== "online") return { eligible: false, reason: "设备离线，无法下发矿工控制任务" };
  if (!agentSupportsMiningControl(agent.agent_version)) return { eligible: false, reason: "需要 RigPilot Agent 0.5.2 或更高版本" };
  const status = miningStatusForAgent(agent);
  if (status?.configured !== true) return { eligible: false, reason: "设备尚未应用飞行表" };
  const hasPendingControl = state.commands.some((command) => command.agent_id === agent.id
    && ["stop_mining", "restart_mining"].includes(command.action)
    && ["queued", "dispatched", "running"].includes(command.status));
  if (hasPendingControl) return { eligible: false, reason: "已有停止或重启挖矿任务正在执行" };
  if (action === "stop_mining") {
    const presentation = miningControlPresentation(agent, status, miningDisplayState(status, agent));
    if (!presentation.stopActive) return { eligible: false, reason: presentation.stopReason || "当前没有可确认运行的矿工进程" };
  } else if (miningStatusUsesHiveos(status)) {
    return { eligible: false, reason: "HiveOS 已停止支持，不能重启该矿工" };
  }
  return { eligible: true, reason: "" };
}

function excludedMiningControlBatchAgents(action) {
  return filteredAgents()
    .map((agent) => ({ agent, eligibility: miningControlBatchEligibility(agent, action) }))
    .filter((item) => !item.eligibility.eligible)
    .map((item) => ({ agent: item.agent, reason: item.eligibility.reason }));
}

function miningControlBatchConfirmMessage(action, selected, excluded) {
  const actionLabel = action === "stop_mining" ? "停止挖矿" : "重启挖矿";
  const names = selected.map((agent) => `${agent.name}（${agent.id}）`).join("\n");
  const shownExcluded = excluded.slice(0, 20);
  const excludedLines = shownExcluded.map((item) => `${item.agent.name}（${item.agent.id}）：${item.reason}`).join("\n");
  const remaining = excluded.length - shownExcluded.length;
  const effectNote = action === "stop_mining"
    ? "停止矿工后会尝试把设备上的 NVML OC 恢复为硬件默认值；控制端保存的 OC 配置不会删除。恢复失败时任务会明确标记失败。"
    : "挖矿会短暂中断。";
  return `确认对以下 ${selected.length} 台设备执行批量${actionLabel}？\n\n${names}\n\n${effectNote}\n\n本次不会执行的设备（${excluded.length} 台）：\n${excludedLines || "无"}${remaining > 0 ? `\n…以及其他 ${remaining} 台设备` : ""}\n\n控制端会逐台完整校验；未勾选设备不受影响。`;
}

function openMiningControlBatchDialog(action) {
  if (!["admin", "operator"].includes(state.user?.role)) return;
  if (!["stop_mining", "restart_mining"].includes(action)) return;
  state.miningControlBatchAction = action;
  const form = $("#miningControlBatchForm");
  form.reset();
  $("#miningControlBatchError").textContent = "";
  $("#miningControlBatchResults").innerHTML = "";
  $("#miningControlBatchResults").classList.add("hidden");
  const isStop = action === "stop_mining";
  $("#miningControlBatchTitle").textContent = isStop ? "批量停止挖矿" : "批量重启挖矿";
  $("#miningControlBatchWarning").textContent = isStop
    ? "逐台明确勾选目标设备。控制端会对每台设备分别执行与单机停止挖矿完全相同的在线、被控端版本与任务冲突校验；停止后 NVML OC 会尝试恢复硬件默认值。"
    : "逐台明确勾选目标设备。控制端会对每台设备分别执行与单机重启挖矿完全相同的在线、被控端版本与任务冲突校验。";
  $("#miningControlBatchSubmitButton").textContent = isStop ? "确认批量停止挖矿" : "确认批量重启挖矿";
  renderMiningControlBatchAgents();
  $("#miningControlBatchDialog").showModal();
}

function renderMiningControlBatchAgents() {
  const action = state.miningControlBatchAction;
  const container = $("#miningControlBatchAgents");
  $("#miningControlBatchError").textContent = "";
  $("#miningControlBatchResults").innerHTML = "";
  $("#miningControlBatchResults").classList.add("hidden");
  const agents = [...filteredAgents()].sort((left, right) => left.name.localeCompare(right.name, "zh-CN"));
  container.innerHTML = agents.length ? agents.map((agent) => {
    const os = osLabel(agent);
    const eligibility = miningControlBatchEligibility(agent, action);
    return `<label class="batch-choice">
      <input type="checkbox" data-mining-control-batch="${escapeHtml(agent.id)}"${eligibility.eligible ? "" : " disabled"}>
      <span><strong>${escapeHtml(agent.name)}</strong><small>${escapeHtml(agent.id)} · ${escapeHtml(statusLabel(agent.status))} · ${escapeHtml(os.short)} · Agent ${escapeHtml(agent.agent_version || "未知")}${eligibility.reason ? ` · ${escapeHtml(eligibility.reason)}` : ""}</small></span>
    </label>`;
  }).join("") : '<div class="batch-choice-empty">当前搜索与状态筛选下没有设备。</div>';
  $$("[data-mining-control-batch]", container).forEach((input) => input.addEventListener("change", syncMiningControlBatchSelection));
  syncMiningControlBatchSelection();
}

function selectedMiningControlBatchItems() {
  return $$("[data-mining-control-batch]:checked", $("#miningControlBatchAgents"))
    .map((input) => state.agents.find((agent) => agent.id === input.dataset.miningControlBatch))
    .filter(Boolean);
}

function syncMiningControlBatchSelection() {
  const selected = selectedMiningControlBatchItems();
  $("#miningControlBatchSelectedCount").textContent = `已选择 ${selected.length} 台`;
  const blocked = selected.map((agent) => ({ agent, eligibility: miningControlBatchEligibility(agent, state.miningControlBatchAction) })).find((item) => !item.eligibility.eligible);
  $("#miningControlBatchSubmitButton").disabled = selected.length === 0 || Boolean(blocked);
  const excluded = excludedMiningControlBatchAgents(state.miningControlBatchAction);
  $("#miningControlBatchEligibilityHint").textContent = blocked
    ? `${blocked.agent.name}：${blocked.eligibility.reason}；请修改勾选后再执行。`
    : excluded.length
      ? `前端预筛选已排除 ${excluded.length} 台设备（离线、被控端过旧或已有未完成任务）；最终结果以控制端逐台校验为准。`
      : "";
}

async function submitMiningControlBatch() {
  if (!["admin", "operator"].includes(state.user?.role)) return;
  const action = state.miningControlBatchAction;
  if (!["stop_mining", "restart_mining"].includes(action)) return;
  const selected = selectedMiningControlBatchItems();
  const errorElement = $("#miningControlBatchError");
  errorElement.textContent = "";
  if (!selected.length) {
    errorElement.textContent = "请明确勾选至少一台设备；系统不会默认全选。";
    return;
  }
  const blocked = selected.map((agent) => ({ agent, eligibility: miningControlBatchEligibility(agent, action) })).find((item) => !item.eligibility.eligible);
  if (blocked) { errorElement.textContent = `${blocked.agent.name}：${blocked.eligibility.reason}`; syncMiningControlBatchSelection(); return; }
  if (!window.confirm(miningControlBatchConfirmMessage(action, selected, excludedMiningControlBatchAgents(action)))) return;
  const button = $("#miningControlBatchSubmitButton");
  button.disabled = true;
  try {
    const response = await api("/api/agents/commands/batch", {
      method: "POST",
      body: { action, agent_ids: selected.map((agent) => agent.id) },
    });
    renderBatchOperationResults(
      $("#miningControlBatchResults"),
      selected.map((agent) => ({ id: agent.id, label: agent.name, detail: agent.id })),
      response,
      ["agent_id", "id"],
    );
    const actionLabel = action === "stop_mining" ? "停止挖矿" : "重启挖矿";
    toast(`批量${actionLabel}已逐台处理：${response.accepted_count || 0} 台进入队列，${response.rejected_count || 0} 台被拒绝`);
    await refreshAll(true);
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    syncMiningControlBatchSelection();
  }
}
async function cancelAgentUpdate(commandId) {
  if (state.user?.role !== "admin" || !commandId) return;
  if (!window.confirm("只允许取消尚未被设备领取的更新任务。确认取消？")) return;
  try {
    await api(`/api/commands/${encodeURIComponent(commandId)}/cancel`, { method: "POST" });
    toast("被控端更新任务已取消");
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function downloadAgentRelease(releaseId, button) {
  const release = state.agentReleases.find((item) => item.id === releaseId);
  if (!release || !state.token) return;
  const authEpoch = state.authEpoch, token = state.token;
  const stillCurrent = () => authEpoch === state.authEpoch && token === state.token;
  button.disabled = true;
  try {
    const archiveType = release.platform === "linux" ? "application/gzip" : "application/zip";
    const response = await fetch(`/api/agent-releases/${encodeURIComponent(release.id)}/download`, {
      headers: { Accept: archiveType, Authorization: `Bearer ${state.token}` },
    });
    if (!response.ok) {
      let detail = `下载失败 (${response.status})`;
      try {
        const document = await response.json();
        if (typeof document?.detail === "string") detail = document.detail;
      } catch {
        // The controller can also return a plain-text reverse-proxy error.
      }
      if (!stillCurrent()) return;
      if (response.status === 401) logout(false);
      toast(detail, "error");
      return;
    }
    if (!stillCurrent()) return;
    const blob = await response.blob();
    if (!stillCurrent()) return;
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = release.file_name || `RigPilotAgent-${release.version}.${release.platform === "linux" ? "tar.gz" : "zip"}`;
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    toast(`RigPilot Agent ${release.version} 已开始下载`);
  } catch (error) {
    if (stillCurrent()) toast(error.message, "error");
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function deleteAgentRelease(releaseId, version) {
  if (state.user?.role !== "admin" || !releaseId) return;
  if (!window.confirm(`确认删除被控端版本 ${version || "未知"}？\n\n只会删除控制端保存的压缩包，不会停止、重启或修改任何被控端和矿工进程。`)) return;
  try {
    await api(`/api/agent-releases/${encodeURIComponent(releaseId)}`, { method: "DELETE" });
    toast(`被控端版本 ${version || "未知"} 已删除`);
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

function managedMinerDefinition(minerType) {
  return state.managedMiners.find((item) => item.id === minerType) || null;
}

function managedMinerVersions(definition) {
  if (!definition) return [];
  const source = Array.isArray(definition.versions) && definition.versions.length
    ? definition.versions
    : (definition.version ? [{
      version: definition.version,
      assets: definition.assets || Object.fromEntries((definition.supported_os || []).map((platform) => [platform, {}])),
    }] : []);
  const unique = [];
  source.forEach((item) => {
    const version = String(item?.version || "").trim();
    if (version && !unique.some((candidate) => candidate.version === version)) unique.push({ ...item, version });
  });
  unique.sort((left, right) => {
    const leftParts = left.version.split(".").map((part) => Number.parseInt(part, 10) || 0);
    const rightParts = right.version.split(".").map((part) => Number.parseInt(part, 10) || 0);
    const length = Math.max(leftParts.length, rightParts.length);
    for (let index = 0; index < length; index += 1) {
      const difference = (rightParts[index] || 0) - (leftParts[index] || 0);
      if (difference) return difference;
    }
    return 0;
  });
  return unique;
}

function managedVersionIsPreview(version) {
  return Boolean(
    version?.prerelease
    || version?.is_prerelease
    || ["beta", "preview", "prerelease", "test"].includes(String(version?.channel || "").toLowerCase())
  );
}

function managedVersionAsset(version, platform) {
  if (!version) return null;
  const assets = version.assets;
  if (Array.isArray(assets)) {
    return assets.find((asset) => [asset?.os_family, asset?.platform, asset?.os].includes(platform)) || null;
  }
  return assets?.[platform] || version?.[platform] || null;
}

function managedAssetFilename(asset) {
  if (!asset) return "";
  if (typeof asset === "string") return asset.split("/").pop() || asset;
  if (asset.filename) return String(asset.filename);
  if (asset.name) return String(asset.name);
  if (asset.download_url || asset.url) return String(asset.download_url || asset.url).split("/").pop() || "";
  return "";
}

function managedVersionEntry(minerType, version) {
  if (!version) return null;
  return managedMinerVersions(managedMinerDefinition(minerType)).find((item) => item.version === String(version)) || null;
}

function managedVersionIsAvailable(minerType, version) {
  if (!version || minerType === "generic") return true;
  if (minerType === "seine" && seineFlightPackages(version).length) return true;
  const entry = managedVersionEntry(minerType, version);
  return Boolean(entry && ["windows", "linux", "linux-nvidia", "linux-amd"].some((platform) => {
    const asset = managedVersionAsset(entry, platform);
    return asset && asset.available !== false;
  }));
}

function managedVersionWslAsset(minerType, version) {
  const entry = managedVersionEntry(minerType, version);
  if (!entry) return null;
  return minerType === "pearlminer"
    ? (managedVersionAsset(entry, "linux-nvidia") || managedVersionAsset(entry, "linux"))
    : managedVersionAsset(entry, "linux");
}

function managedVersionOptionLabel(version, minerType = "") {
  const platforms = [];
  if (managedVersionAsset(version, "windows")) platforms.push("Windows");
  if (managedVersionAsset(version, "linux-nvidia") || managedVersionAsset(version, "linux")) platforms.push(minerType === "pearlminer" ? "Linux NVIDIA" : "Linux");
  if (managedVersionAsset(version, "linux-amd")) platforms.push("Linux AMD");
  const definition = managedMinerDefinition(minerType);
  const isPreview = managedVersionIsPreview(version);
  let releaseLabel = isPreview ? "测试版" : "";
  if (minerType === "seine" && seineFlightPackages(version.version).length) {
    releaseLabel = "含上传构建";
  }
  if (minerType === "pearlminer" && definition) {
    const stableVersion = String(definition.latest_version || definition.version || "");
    if (String(version.version) === stableVersion) {
      releaseLabel = "默认稳定版";
    } else if (isPreview) {
      const latestPreview = managedMinerVersions(definition).find(managedVersionIsPreview);
      releaseLabel = latestPreview?.version === version.version ? "最新测试版" : "历史测试版";
    } else {
      releaseLabel = "历史稳定版";
    }
  }
  const versionLabel = `${version.version}${releaseLabel ? ` · ${releaseLabel}` : ""}`;
  return `${versionLabel}${platforms.length ? ` · ${platforms.join(" / ")}` : " · 暂无平台包"}`;
}

function managedMinerLabel(minerType, version = "") {
  if (RETIRED_MINER_LABELS[minerType]) return `${RETIRED_MINER_LABELS[minerType]}${version ? ` ${version}` : ""} · 已退休`;
  const definition = managedMinerDefinition(minerType);
  if (definition) return `${definition.name}${version ? ` ${version}` : ` ${definition.latest_version || definition.version || ""}`}`.trim();
  const adapter = minerAdapterDefinition(minerType);
  if (adapter) return `${adapter.display_name || adapter.miner_type}${version ? ` ${version}` : ""}`.trim();
  return minerType === "generic" ? "旧版自定义" : `${minerType || "未知矿工"}${version ? ` ${version}` : ""} · 已移除或不受支持`;
}

function flightSheetVersionIssue(sheet) {
  const retiredType = [sheet?.miner_type, sheet?.secondary?.miner_type].find((type) => RETIRED_MINER_LABELS[type]);
  if (retiredType) return `${RETIRED_MINER_LABELS[retiredType]} 已退休；历史记录保留，请创建副本并明确选择当前支持的矿工后下发`;
  if (!sheet || sheet.miner_type === "generic") return "";
  for (const leg of [sheet, sheet.secondary]) {
    if (leg?.miner_type === "seine" && leg.miner_package_id && !seineFlightPackages(leg.miner_version).some((item) => item.id === leg.miner_package_id)) {
      return "绑定的 Seine 上传构建已缺失、不可用或与版本不匹配；请明确重新选择矿工包来源，不会自动改用官方包";
    }
  }
  const primaryPinned = sheet.miner_type === "seine" && Boolean(sheet.miner_package_id);
  if (!minerAdapterDefinition(sheet.miner_type) || (!primaryPinned && !managedMinerDefinition(sheet.miner_type))) {
    return "该历史飞行表使用已移除或不受支持的矿工，必须改用当前支持的托管矿工后才能下发";
  }
  if (!primaryPinned && sheet.miner_version && !managedVersionIsAvailable(sheet.miner_type, sheet.miner_version)) {
    return `${managedMinerLabel(sheet.miner_type, sheet.miner_version)} 当前在控制端版本目录中不可用`;
  }
  const secondary = sheet.secondary;
  const secondaryPinned = secondary?.miner_type === "seine" && Boolean(secondary.miner_package_id);
  if (secondary?.miner_type && secondary.miner_type !== "generic" && (!minerAdapterDefinition(secondary.miner_type) || (!secondaryPinned && !managedMinerDefinition(secondary.miner_type)))) {
    return "该历史飞行表的第二矿工已移除或不受支持，必须改用当前支持的托管矿工后才能下发";
  }
  if (!secondaryPinned && secondary?.miner_version && !managedVersionIsAvailable(secondary.miner_type || "srbminer", secondary.miner_version)) {
    return `${managedMinerLabel(secondary.miner_type || "srbminer", secondary.miner_version)} 当前在控制端版本目录中不可用`;
  }
  return "";
}

function renderFlightSheets() {
  const body = $("#flightTableBody");
  if (!body) return;
  const isAdmin = state.user?.role === "admin";
  const canEdit = state.user?.role === "admin" || state.user?.role === "operator";
  body.innerHTML = state.flightSheets.map((sheet) => {
    const secondary = sheet.secondary && typeof sheet.secondary === "object" ? sheet.secondary : null;
    const primaryWalletName = sheet.wallet_name || "钱包已移除";
    const primaryWalletAddress = sheet.wallet_address || "";
    const secondaryWalletName = secondary?.wallet_name || "钱包已移除";
    const secondaryWalletAddress = secondary?.wallet_address || "";
    const primaryMiner = managedMinerLabel(sheet.miner_type || "generic", sheet.miner_version, sheet.miner_package_id) + (sheet.miner_type === "seine" && sheet.miner_package_id ? " · 上传构建" : "");
    const secondaryMiner = secondary ? managedMinerLabel(secondary.miner_type || "generic", secondary.miner_version, secondary.miner_package_id) + (secondary.miner_type === "seine" && secondary.miner_package_id ? " · 上传构建" : "") : "";
    const versionIssue = flightSheetVersionIssue(sheet);
    const role = (label) => secondary ? `<span class="flight-card-leg-role">${label}</span>` : "";
    return `<tr class="flight-sheet-card ${secondary ? "has-secondary" : "single-miner"}">
      <td class="flight-card-title"><strong title="${escapeHtml(sheet.name)}">${escapeHtml(sheet.name)}</strong>${sheet.miner_type === "hiveos" ? '<span class="hiveos-badge">HiveOS</span>' : ""}</td>
      <td class="flight-card-coin flight-card-data" data-label="币种"><div class="flight-card-leg-list"><div class="flight-card-leg-row">${role("主挖")}<strong>${escapeHtml(sheet.coin)}</strong></div>${secondary ? `<div class="flight-card-leg-row">${role("副挖")}<strong>${escapeHtml(secondary.coin)}</strong></div>` : ""}</div></td>
      <td class="flight-card-miner flight-card-data" data-label="挖矿软件"><div class="flight-card-leg-list"><div class="flight-card-leg-row">${role("主挖")}<code title="${escapeHtml(primaryMiner)}">${escapeHtml(primaryMiner)}</code></div>${secondary ? `<div class="flight-card-leg-row">${role("副挖")}<code title="${escapeHtml(secondaryMiner)}">${escapeHtml(secondaryMiner)}</code></div>` : ""}</div></td>
      <td class="flight-card-wallet flight-card-data" data-label="钱包"><div class="flight-card-leg-list"><div class="flight-card-leg-row">${role("主挖")}<div class="flight-card-wallet-copy"><strong class="flight-card-wallet-name" title="${escapeHtml(primaryWalletName)}">${escapeHtml(primaryWalletName)}</strong><code class="address-cell" title="${escapeHtml(primaryWalletAddress)}">${escapeHtml(primaryWalletAddress || "公开地址不可用")}</code></div></div>${secondary ? `<div class="flight-card-leg-row">${role("副挖")}<div class="flight-card-wallet-copy"><strong class="flight-card-wallet-name" title="${escapeHtml(secondaryWalletName)}">${escapeHtml(secondaryWalletName)}</strong><code class="address-cell" title="${escapeHtml(secondaryWalletAddress)}">${escapeHtml(secondaryWalletAddress || "公开地址不可用")}</code></div></div>` : ""}</div></td>
      <td class="flight-card-pool flight-card-data" data-label="矿池"><div class="flight-card-leg-list"><div class="flight-card-leg-row">${role("主挖")}<code class="address-cell" title="${escapeHtml(sheet.pool_url)}">${escapeHtml(sheet.pool_url)}</code></div>${secondary ? `<div class="flight-card-leg-row">${role("副挖")}<code class="address-cell" title="${escapeHtml(secondary.pool_url)}">${escapeHtml(secondary.pool_url)}</code></div>` : ""}</div></td>
      <td class="flight-card-devices" data-label="使用设备"><strong class="flight-device-count">${Number(sheet.configured_device_count ?? sheet.device_count) || 0}</strong><small class="table-subline">已配置设备</small></td>
      <td class="flight-card-actions">${versionIssue ? `<span class="flight-card-warning" role="img" aria-label="${escapeHtml(versionIssue)}" title="${escapeHtml(versionIssue)}">!</span>` : ""}${canEdit ? `<button class="text-button" type="button" data-edit-flight="${escapeHtml(sheet.id)}">编辑</button>` : ""}${isAdmin ? `<button class="text-button danger-text" type="button" data-delete-flight="${escapeHtml(sheet.id)}">删除</button>` : ""}</td>
    </tr>`;
  }).join("");
  $("#flightEmpty").classList.toggle("hidden", state.flightSheets.length > 0);
  setTableVisible(body.closest("table"), state.flightSheets.length > 0);
  $$('[data-edit-flight]', body).forEach((button) => button.addEventListener("click", () => openFlightDialog(button.dataset.editFlight)));
  $$('[data-delete-flight]', body).forEach((button) => button.addEventListener("click", () => deleteResource("flight", button.dataset.deleteFlight)));
}

function gpuOcEditSummary(setting) {
  const labels = { power_limit_w: "Power Limit", core_clock_offset_mhz: "Core Offset", core_clock_lock_mhz: "Lock Core",
    memory_clock_offset_mhz: "Memory Offset", memory_clock_lock_mhz: "Lock Memory", fan_percent: "Fan" };
  const reset = Object.keys(labels).filter((field) => Object.hasOwn(setting, field) && setting[field] === null)
    .map((field) => `${labels[field]} 恢复默认`);
  return [gpuOcParameterSummary(setting), ...reset].filter(Boolean).join(" · ");
}

function gpuOcParameterSummary(setting, localized = false) {
  const labels = localized
    ? { power_limit_w: "功耗上限", core_clock_offset_mhz: "核心偏移", core_clock_lock_mhz: "核心锁频",
      memory_clock_offset_mhz: "显存偏移", memory_clock_lock_mhz: "显存锁频", fan_percent: "风扇" }
    : { power_limit_w: "Power Limit", core_clock_offset_mhz: "Core Offset", core_clock_lock_mhz: "Lock Core",
      memory_clock_offset_mhz: "Memory Offset", memory_clock_lock_mhz: "Lock Memory", fan_percent: "Fan" };
  const values = [];
  if (setting.power_limit_w != null) values.push(`${labels.power_limit_w} ${setting.power_limit_w} W`);
  if (setting.core_clock_offset_mhz != null) values.push(`${labels.core_clock_offset_mhz} ${setting.core_clock_offset_mhz >= 0 ? "+" : ""}${setting.core_clock_offset_mhz} MHz`);
  if (setting.core_clock_lock_mhz != null) values.push(`${labels.core_clock_lock_mhz} ${setting.core_clock_lock_mhz} MHz`);
  if (setting.memory_clock_offset_mhz != null) values.push(`${labels.memory_clock_offset_mhz} ${setting.memory_clock_offset_mhz >= 0 ? "+" : ""}${setting.memory_clock_offset_mhz} MHz`);
  if (setting.memory_clock_lock_mhz != null) values.push(`${labels.memory_clock_lock_mhz} ${setting.memory_clock_lock_mhz} MHz`);
  if (setting.fan_percent != null) values.push(`${labels.fan_percent} ${setting.fan_percent}%`);
  return values.join(" · ");
}

function gpuMetricIndex(gpu) {
  const raw = gpu?.index;
  const index = typeof raw === "string" && /^\d+$/.test(raw) ? Number(raw) : raw;
  return Number.isInteger(index) && index >= 0 && index <= 31 ? index : null;
}

function gpuMetricIsNvidia(gpu) {
  return gpuReportedVendor(gpu) === "nvidia";
}

function gpuMetricUuid(gpu) {
  const uuid = typeof gpu?.uuid === "string" ? gpu.uuid.trim() : "";
  return uuid.length >= 4 && uuid.length <= 128 && !/[\u0000-\u001f\u007f]/.test(uuid) ? uuid : null;
}

function gpuOcCapabilities(gpu) {
  if (Array.isArray(gpu?.oc_capabilities)) {
    return gpu.oc_capabilities.filter((field) => gpuOcFields.includes(field));
  }
  // Agent <=0.6.8 did not report a capability probe.  The nvidia-smi power
  // range is the only existing telemetry that confirms a useful read path;
  // do not guess support for offset, lock, or fan setters.
  const minimum = Number(gpu?.power_min_limit_w);
  const maximum = Number(gpu?.power_max_limit_w);
  return Number.isFinite(minimum) && Number.isFinite(maximum) && maximum >= minimum && maximum > 0
    ? ["power_limit_w"]
    : [];
}

function gpuOcProfiles(agent) {
  const status = miningStatusForAgent(agent);
  return Array.isArray(status?.gpu_oc_profiles)
    ? status.gpu_oc_profiles.filter((profile) => profile && typeof profile === "object" && Array.isArray(profile.settings))
    : [];
}

function gpuDesiredOcEntry(agent, gpu) {
  const desired = Array.isArray(agent?.desired_gpu_oc) ? agent.desired_gpu_oc : [];
  const uuid = gpuMetricUuid(gpu);
  return desired.find((entry) => (
    entry && typeof entry === "object"
    && entry.setting && typeof entry.setting === "object"
    && String(entry.gpu_uuid || "") === uuid
  )) || null;
}

function gpuMissingDesiredOcEntries(agent) {
  const desired = Array.isArray(agent?.desired_gpu_oc) ? agent.desired_gpu_oc : [];
  const gpus = agent?.metrics?.gpus;
  if (!Array.isArray(gpus)) return [];
  const reportedUuids = new Set(gpus.map(gpuMetricUuid).filter(Boolean));
  return desired.filter((entry) => {
    const uuid = gpuMetricUuid({ uuid: entry?.gpu_uuid });
    return Boolean(uuid && !reportedUuids.has(uuid));
  });
}

function gpuStandaloneOcSetting(agent, gpu) {
  const desired = gpuDesiredOcEntry(agent, gpu);
  if (desired) return desired.setting;
  const profile = gpuOcProfiles(agent).find((candidate) => (
    String(candidate?.source || "").toLowerCase() === "nvml"
    && String(candidate?.state || "").toLowerCase() === "applied_record"
  ));
  if (!profile) return null;
  return profile.settings.find((setting) => (
    setting && typeof setting === "object" && gpuOcSettingMatches(profile, setting, gpu)
  )) || null;
}

function compactGpuModel(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function gpuOcSettingMatches(profile, setting, gpu) {
  const source = String(profile?.source || "").toLowerCase();
  const index = gpuMetricIndex(gpu);
  if (source === "nvml") {
    const settingUuid = gpuMetricUuid(setting);
    const currentUuid = gpuMetricUuid(gpu);
    if (settingUuid || profile?.identity_complete === true) {
      return Boolean(settingUuid && currentUuid && settingUuid === currentUuid);
    }
    return Number(setting?.index) === index;
  }
  if (source !== "srbminer_cli") return false;
  if (typeof setting?.model_match === "string") {
    const matcher = compactGpuModel(setting.model_match);
    return matcher.length >= 2 && compactGpuModel(gpu?.name).includes(matcher);
  }
  if (setting?.nvml_index != null) return Number(setting.nvml_index) === index;
  if (setting?.uuid && gpuMetricUuid(gpu)) return String(setting.uuid) === gpuMetricUuid(gpu);
  if (setting?.pci_bus_id && gpu?.pci_bus_id) {
    return String(setting.pci_bus_id).toLowerCase() === String(gpu.pci_bus_id).toLowerCase();
  }
  // Legacy explicit SRBMiner IDs may differ from NVML indices on mixed-vendor
  // systems.  This fallback preserves existing all-NVIDIA rigs; new Agents can
  // report nvml_index/UUID/PCI identity to remove that ambiguity.
  return source === "srbminer_cli" && Number(setting?.index) === index;
}

function gpuActiveOcMarkup(agent, gpu) {
  const groups = new Map();
  const addRecord = (setting, label) => {
    const summary = gpuOcParameterSummary(setting, true);
    if (!summary) return;
    // Compare the exact reported fields rather than rounded display text.
    // An explicit reset, a missing field, and the value zero remain distinct.
    const key = JSON.stringify(gpuOcFields.map((field) => [
      Object.hasOwn(setting, field), setting[field],
    ]));
    if (!groups.has(key)) groups.set(key, { summary, sources: [] });
    const group = groups.get(key);
    if (!group.sources.includes(label)) group.sources.push(label);
  };
  const desired = gpuDesiredOcEntry(agent, gpu);
  const replayBlock = desired?.replay_block;
  const replayBlocked = replayBlock && typeof replayBlock === "object"
    && typeof desired?.intent_id === "string" && replayBlock.intent_id === desired.intent_id
    && ["hardware_reset_required", "hardware_fault", "recovery_failed", "state_persistence_failed", "legacy_reset_required"].includes(replayBlock.reason);
  if (desired) addRecord(desired.setting, replayBlocked ? "控制端已保存 · 自动重试已暂停" : "控制端已保存");
  const miningState = String(miningStatusForAgent(agent)?.state || "").toLowerCase();
  gpuOcProfiles(agent).forEach((profile) => {
    const source = String(profile.source || "").toLowerCase();
    const profileState = String(profile.state || "").toLowerCase();
    const minerCliSource = source === "srbminer_cli";
    if (minerCliSource && profileState !== "declared") return;
    if (source === "nvml" && profileState !== "applied_record") return;
    if (minerCliSource && !["running", "active", "starting"].includes(miningState)) return;
    const label = source === "srbminer_cli" ? "SRBMiner 参数声明" : source === "nvml" ? "NVML 最近成功下发" : "OC 参数";
    profile.settings.filter((setting) => setting && typeof setting === "object" && gpuOcSettingMatches(profile, setting, gpu))
      .forEach((setting) => addRecord(setting, label));
  });
  if (!groups.size) return '<div class="device-gpu-oc-records is-empty"><span>OC 记录</span><small>暂无已保存、参数声明或最近成功下发记录</small></div>';
  let replayMarkup = "";
  if (replayBlocked) {
    const recoveryLabels = { rolled_back: "上次尝试：参数已回滚", defaults_restored: "上次尝试：参数恢复默认",
      partial: "上次尝试：参数部分恢复", failed: "上次尝试：参数恢复未完成" };
    const facts = [replayBlock.reason === "legacy_reset_required" ? "旧 Agent 未上报恢复结果"
      : Object.hasOwn(recoveryLabels, replayBlock.recovery_status) ? recoveryLabels[replayBlock.recovery_status] : "恢复结果未确认"];
    if (replayBlock.hardware_reset_required === true) facts.push("上次尝试报告需要设备重置");
    if (replayBlock.hardware_identity_confirmed === false) facts.push("故障回执未确认硬件身份");
    if (replayBlock.state_reconciled === false) facts.push("Agent 记录同步未确认");
    const resetFields = new Set((Array.isArray(replayBlock.confirmed_reset) ? replayBlock.confirmed_reset : [])
      .filter((row) => row?.gpu_uuid === desired.gpu_uuid && Array.isArray(row.reset))
      .flatMap((row) => row.reset.filter((field) => gpuOcFields.includes(field))));
    if (resetFields.size) facts.push(`上次确认 ${resetFields.size} 项参数已复位`);
    const error = typeof replayBlock.error === "string" ? replayBlock.error : "该设置的自动重试已暂停";
    replayMarkup = `<div class="device-gpu-oc-record"><strong>自动重试已暂停</strong><span class="device-gpu-oc-parameters" title="${escapeHtml(error)}">${escapeHtml(error)}</span>`
      + `<div class="device-gpu-oc-sources">${facts.map((fact) => `<span class="device-gpu-oc-source">${escapeHtml(fact)}</span>`).join("")}</div></div>`;
  }
  return `<div class="device-gpu-oc-records">${[...groups.values()].map((group) => (
    `<div class="device-gpu-oc-record gpu-active-oc"><strong>OC 记录</strong><span class="device-gpu-oc-parameters">${group.summary.split(" · ").map((parameter) => `<span class="device-gpu-oc-parameter">${escapeHtml(parameter)}</span>`).join("")}</span>`
    + (group.sources.length > 1 ? '<span class="device-gpu-oc-match">参数一致</span>' : "")
    + `<div class="device-gpu-oc-sources">${group.sources.map((label) => `<span class="device-gpu-oc-source">${escapeHtml(label)}</span>`).join("")}</div>`
    + '</div>'
  )).join("")}${replayMarkup}</div>`;
}

function deviceNetworkMarkup(agent, detailed = false) {
  const publicIp = agent?.public_ip || "—";
  const localIps = Array.isArray(agent?.local_ip_addresses)
    ? agent.local_ip_addresses.filter((value) => typeof value === "string" && value)
    : agent?.local_ip ? [agent.local_ip] : [];
  const localLabel = localIps.length ? localIps.join(detailed ? "、" : ", ") : "—";
  return `<div class="device-network${detailed ? " detailed" : ""}"><span><small>公网</small><strong>${escapeHtml(publicIp)}</strong></span><span><small>局域网</small><strong title="${escapeHtml(localLabel)}">${escapeHtml(localLabel)}</strong></span></div>`;
}

function gpuTextValue(value, fallback = "未知") {
  const text = typeof value === "string" ? value.trim() : "";
  return text && !/[\u0000-\u001f\u007f]/.test(text) ? text : fallback;
}

function gpuNumberValue(value) {
  if (!["number", "string"].includes(typeof value) || typeof value === "string" && !value.trim()) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function gpuMemoryLabel(gpu) {
  const total = gpuNumberValue(gpu?.memory_total_mib);
  const used = gpuNumberValue(gpu?.memory_used_mib);
  const reportedPercent = gpuNumberValue(gpu?.memory_percent);
  const percent = reportedPercent ?? (total > 0 && used !== null && used >= 0 ? used / total * 100 : null);
  if (total !== null || used !== null) {
    return `${metric(used, "")} / ${metric(total, "")} MiB${percent === null ? "" : ` · ${metric(percent)}`}`;
  }
  return percent === null ? "—" : metric(percent);
}

function gpuPowerLimitsLabel(gpu) {
  const values = [gpu?.power_min_limit_w, gpu?.power_default_limit_w, gpu?.power_max_limit_w]
    .map(gpuNumberValue);
  return values.some((value) => value !== null)
    ? `${values.map((value) => metric(value, "")).join(" / ")} W` : "未上报";
}

function gpuNormalizedModel(name) {
  return gpuTextValue(name, "")
    .toLowerCase()
    .replace(/\b(?:nvidia|geforce)\b/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function gpuMemoryIdentityParts(gpu) {
  const reportedVendor = gpuTextValue(gpu?.memory_vendor, "");
  const reportedType = gpuTextValue(gpu?.memory_type, "");
  if (reportedVendor && reportedType) return [reportedVendor, reportedType];
  if (reportedVendor) return [reportedVendor];
  if (reportedType) return [`${reportedType}（颗粒厂商未上报）`];

  // Explicit collection metadata is authoritative even when no value was
  // obtained. Older reports can still use the published model specification.
  if (gpuTextValue(gpu?.memory_identity_source, "")
      || gpuTextValue(gpu?.memory_identity_status, "")
      || gpuTextValue(gpu?.memory_identity_unavailable_reason, "")) {
    return ["显存颗粒未上报"];
  }

  const model = gpuNormalizedModel(gpu?.name);
  const memoryType = gpuModelMemoryTypes[model];
  return memoryType ? [`${memoryType}（颗粒厂商未上报）`] : ["显存颗粒未上报"];
}

function gpuMemoryIdentityProvenance(gpu) {
  const source = gpuTextValue(gpu?.memory_identity_source, "");
  const status = gpuTextValue(gpu?.memory_identity_status, "");
  const sources = {
    nvapi: "NVAPI 驱动上报", nvml_private: "NVML 驱动查询",
    verified_vbios: "既有 VBIOS 对应记录", product_spec: "型号规格",
  };
  const statuses = {complete: "完整", partial: "部分", unavailable: "未取得"};
  return [
    source ? `显存来源：${Object.hasOwn(sources, source) ? sources[source] : source}` : "",
    status ? `采集状态：${Object.hasOwn(statuses, status) ? statuses[status] : status}` : "",
  ].filter(Boolean).join(" · ");
}

function gpuBoardLabel(gpu) {
  // The Agent and this offline asset share one generated PCI registry.
  // A registered subsystem vendor ID also resolves for older online Agents.
  const registry = globalThis.RigPilotPciIdentity;
  const boardId = registry?.normalizeId(gpu?.board_vendor_id)
    || gpuTextValue(gpu?.board_vendor_id, "").toLowerCase();
  const board = registry
    ? registry.boardVendorLabel(gpu?.board_vendor_id, gpu?.board_vendor)
    : gpuTextValue(gpu?.board_vendor, "");
  return gpuTextValue(
    board,
    boardId ? `未知品牌 (ID ${boardId.toUpperCase()})` : "品牌未上报",
  );
}

function gpuMiningHashrateMarkup(agent, gpu) {
  const snapshot = gpuMiningHashrateSnapshot(agent, gpu);
  if (!snapshot.active) return "";
  if (snapshot.readings.length) {
    const rows = snapshot.readings.map((reading) => (
      `<span class="gpu-mining-hashrate-row"><small>${escapeHtml(reading.identity)}</small>`
      + `<strong>${escapeHtml(reading.amount)}</strong></span>`
    )).join("");
    return `<div class="gpu-mining-hashrate"><b>挖矿算力</b><div>${rows}</div></div>`;
  }
  const unavailableLabels = {
    not_applicable: "XMRig 当前仅上报 CPU 总算力，不提供逐卡 GPU 算力",
    api_unavailable: "矿工本地 API 暂无响应，逐卡算力暂不可用",
    identity_unmatched: "矿工显卡身份与 NVML 未匹配，已拒绝显示可能错卡的算力",
    not_reported: "矿工 API 当前未返回逐卡算力（启动中或该算法未提供）",
  };
  const reason = miningStatusUsesHiveos(snapshot.status)
    ? "尚无可核验的逐卡对应关系；总算力保留，逐卡算力与能效暂不推算"
    : unavailableLabels[snapshot.status.gpu_hashrate_status];
  return reason
    ? `<div class="gpu-mining-hashrate-unavailable">${escapeHtml(reason)}</div>`
    : "";
}


function gpuOcButtonMarkup(agent, gpu, canOperate) {
  const index = gpuMetricIndex(gpu);
  if (!canOperate || index === null || !gpuMetricIsNvidia(gpu) || !gpuMetricUuid(gpu)
      || !gpuOcCapabilities(gpu).length || gpu.driver_available === false) return "";
  const online = agent.status === "online";
  const maintenanceReason = deviceRuntimePreparationReason(agent);
  const supportsSafePerGpuOc = agentVersionAtLeast(agent.agent_version, [0, 6, 16]);
  return `<button class="btn btn-secondary btn-small" type="button" data-gpu-uuid="${escapeHtml(gpuMetricUuid(gpu))}" data-device-gpu-oc="${index}"${online && supportsSafePerGpuOc && !maintenanceReason ? "" : ` disabled title="${escapeHtml(maintenanceReason || (online ? "逐卡累计 OC 需要 Agent 0.6.16 或更高版本" : "设备离线时不能下发 OC"))}"`}>OC 设置</button>`;
}

function gpuMissingDesiredOcMarkup(agent, canOperate) {
  const entries = gpuMissingDesiredOcEntries(agent);
  if (!entries.length) return "";
  const supportsSafePerGpuOc = agentVersionAtLeast(agent.agent_version, [0, 6, 16]);
  const canForget = canOperate && agent.status === "online" && supportsSafePerGpuOc;
  const disabledReason = agent.status !== "online"
    ? "设备离线时不能确认 GPU 已缺席"
    : "删除缺席记录需要 Agent 0.6.16 或更高版本";
  return `<section class="drawer-section device-detail-gpu-missing"><div class="drawer-section-head"><div><h4>已缺席 OC 记录</h4><small>仅按 GPU UUID 判断；双确认后只删除控制端记录，不下发硬件恢复任务</small></div></div>${entries.map((entry) => {
    const uuid = gpuMetricUuid({ uuid: entry?.gpu_uuid });
    const index = Number(entry?.setting?.index);
    const name = String(entry?.name || `GPU ${Number.isInteger(index) ? index : "—"}`);
    const summary = gpuOcParameterSummary(entry?.setting || {});
    const action = canOperate
      ? `<button class="btn btn-secondary btn-small danger-text" type="button" data-forget-missing-gpu-oc="${escapeHtml(uuid)}"${canForget ? "" : ` disabled title="${escapeHtml(disabledReason)}"`}>删除缺席记录</button>`
      : "";
    return `<div class="gpu-card"><div class="gpu-card-head"><strong>${escapeHtml(name)}</strong><div class="gpu-card-actions">${action}</div></div><div class="gpu-stats"><span>原 Index ${Number.isInteger(index) ? index : "—"}</span><span>${escapeHtml(summary || "已保存 NVML OC 参数")}</span></div><div class="gpu-hardware"><span title="${escapeHtml(uuid)}">UUID ${escapeHtml(uuid)}</span></div></div>`;
  }).join("")}</section>`;
}

function fillDeviceGpuOcControls(form, target, gpu, currentSetting) {
  $$('[data-device-gpu-control]', form).forEach((label) => {
    const field = label.dataset.deviceGpuControl;
    const supported = target.capabilities.includes(field);
    label.classList.toggle("hidden", !supported);
    const input = $("input", label);
    if (!input) return;
    input.disabled = !supported;
    const currentValue = currentSetting?.[field];
    input.value = supported && currentValue != null ? String(currentValue) : "";
    if (field === "power_limit_w") {
      const range = gpuPowerRange(gpu);
      input.min = String(range.minimum ?? 0);
      input.max = range.maximum === null ? "" : String(range.maximum);
    }
    if (field === "fan_percent") {
      const hardwareMinimum = Number(gpu.fan_min_percent);
      const hardwareMaximum = Number(gpu.fan_max_percent);
      const minimum = Number.isFinite(hardwareMinimum) ? Math.max(30, hardwareMinimum) : 30;
      const maximum = Number.isFinite(hardwareMaximum) ? Math.min(100, hardwareMaximum) : 100;
      input.min = String(minimum);
      input.max = String(maximum);
      const hint = $("small", label);
      if (hint) hint.textContent = supported
        ? `NVML 手动风扇 ${minimum}-${maximum}%；停止挖矿或删除已保存 OC 后恢复驱动默认策略`
        : "NVML 手动风扇；当前显卡未确认支持";
    }
  });
}

function openDeviceGpuOc(index, selectedAgent = null) {
  const agent = selectedAgent || state.selectedAgent;
  const canOperate = state.user?.role === "operator" || state.user?.role === "admin";
  if (!agent || !canOperate) {
    toast("需要运维人员或管理员权限", "error");
    return;
  }
  if (agent.status !== "online") {
    toast("设备离线时不能下发 GPU OC", "error");
    return;
  }
  if (deviceRuntimePreparationReason(agent)) { toast(deviceRuntimePreparationReason(agent), "error"); return; }
  if (!agentVersionAtLeast(agent.agent_version, [0, 6, 16])) {
    toast("逐卡累计 OC 需要先将该设备升级到 Agent 0.6.16 或更高版本", "error");
    return;
  }
  const gpus = Array.isArray(agent.metrics?.gpus) ? agent.metrics.gpus : [];
  const gpu = gpus.find((candidate) => gpuMetricIndex(candidate) === index && gpuMetricIsNvidia(candidate) && gpuMetricUuid(candidate));
  if (!gpu) {
    toast("该 GPU 已不在设备最新上报列表中，请刷新后重试", "error");
    return;
  }
  if (gpu.driver_available === false) {
    toast("显卡驱动异常，当前不能下发 OC", "error");
    return;
  }
  const form = $("#deviceGpuOcForm");
  form.reset();
  const capabilities = gpuOcCapabilities(gpu);
  if (!capabilities.length) {
    toast("该 GPU 未确认任何可用的 NVML OC 参数", "error");
    return;
  }
  const desiredEntry = gpuDesiredOcEntry(agent, gpu);
  state.deviceGpuOcTarget = { agentId: agent.id, index, uuid: gpuMetricUuid(gpu), name: gpu.name || `GPU ${index}`, capabilities, hasSaved: Boolean(desiredEntry) };
  $("#deviceGpuOcIndex").value = String(index);
  $("#deviceGpuOcTarget").textContent = `GPU ${index} · ${state.deviceGpuOcTarget.name}`;
  $("#deviceGpuOcTitle").textContent = `GPU ${index} OC 设置`;
  $("#deviceGpuOcError").textContent = "";
  const currentSetting = gpuStandaloneOcSetting(agent, gpu);
  state.deviceGpuOcTarget.initialSetting = { ...(currentSetting || {}) };
  fillDeviceGpuOcControls(form, state.deviceGpuOcTarget, gpu, currentSetting);
  const hasCurrentSetting = Boolean(currentSetting);
  $("#deviceGpuOcCapabilityHint").textContent = desiredEntry
    ? `已载入控制端为该卡保存的参数；停止挖矿只恢复硬件默认值，不会删除这里的配置。清空已填字段会恢复该项默认；原本空白且未填写的字段保持不变，0是数值。`
    : hasCurrentSetting
      ? `已载入 Agent 本地的旧应用记录；控制端尚未保存这组参数，确认下发后才会持久保存。`
      : `仅显示该显卡运行时确认支持的 ${capabilities.length} 项 NVML 参数。`;
  $("#deleteDeviceGpuOcButton").classList.toggle("hidden", !desiredEntry);
  setDeviceGpuOcPending(state.deviceGpuOcTarget, false);
  $("#deviceGpuOcDialog").showModal();
}

function setDeviceGpuOcPending(target, pending) {
  target.pending = pending;
  if (state.deviceGpuOcTarget === target) {
    $("#applyDeviceGpuOcButton").disabled = pending;
    $("#deleteDeviceGpuOcButton").disabled = pending;
  } else if (state.deviceGpuOcBatchTarget === target) {
    syncDeviceGpuOcBatchSelection();
  }
}

function gpuOcNumberIsValid(value, field) {
  return Number.isFinite(value) && (field !== "power_limit_w" || value > 0);
}

function gpuPowerRange(gpu) {
  const read = (raw) => {
    if (!["number", "string"].includes(typeof raw)
        || typeof raw === "string" && !raw.trim()) return null;
    const value = Number(raw);
    return Number.isFinite(value) && value >= 0 ? value : null;
  };
  const minimum = read(gpu?.power_min_limit_w);
  const reportedMaximum = read(gpu?.power_max_limit_w);
  const maximum = reportedMaximum > 0 ? reportedMaximum : null;
  if (minimum !== null && maximum !== null && minimum > maximum) return { minimum: null, maximum: null };
  return { minimum, maximum };
}

function collectDeviceGpuOcSetting(target = state.deviceGpuOcTarget, form = $("#deviceGpuOcForm")) {
  if (!target || !Number.isInteger(target.index)) throw new Error("目标 GPU 已失效，请关闭后重试。");
  const setting = { index: target.index };
  const fields = target.capabilities.filter((field) => gpuOcFields.includes(field));
  fields.forEach((field) => {
    const input = $(`[data-device-gpu-field=${field}]`, form);
    if (input.validity?.badInput) throw new Error("请输入有效的 OC 数值，或清空以恢复该项默认。");
    const raw = input.value.trim();
    if (raw !== "") {
      const value = Number(raw);
      if (!gpuOcNumberIsValid(value, field)) throw new Error("请输入有效的 OC 数值；功耗上限须为正的有限数值。");
      setting[field] = value;
    }
    else if (target.initialSetting?.[field] != null) setting[field] = null;
  });
  if (Object.keys(setting).length === 1) throw new Error("至少填写或明确清空一项 OC 参数。");
  return setting;
}

async function submitDeviceGpuOc(target, form, error, button) {
  if (target?.pending || button.disabled) return;
  const agent = target ? state.agents.find((item) => item.id === target.agentId) : null;
  const canOperate = state.user?.role === "operator" || state.user?.role === "admin";
  error.textContent = "";
  if (!target || !agent || state.deviceGpuOcTarget !== target) {
    error.textContent = "设备或 GPU 已改变，请重新选择。";
    return;
  }
  if (!canOperate || agent.status !== "online") {
    error.textContent = canOperate ? "设备已离线，不能下发 GPU OC。" : "当前账号没有下发权限。";
    return;
  }
  const preparationReason = deviceRuntimePreparationReason(agent);
  if (preparationReason) { error.textContent = preparationReason; return; }
  if (!agentVersionAtLeast(agent.agent_version, [0, 6, 16])) {
    error.textContent = "逐卡累计 OC 需要 Agent 0.6.16 或更高版本。";
    return;
  }
  const currentGpu = Array.isArray(agent.metrics?.gpus)
    ? agent.metrics.gpus.find((gpu) => gpuMetricIndex(gpu) === target.index && gpuMetricIsNvidia(gpu))
    : null;
  if (!currentGpu || gpuMetricUuid(currentGpu) !== target.uuid
      || !gpuOcCapabilities(currentGpu).length) {
    error.textContent = "GPU Index、UUID 或支持能力已变化，请刷新设备后重新选择。";
    return;
  }
  if (currentGpu.driver_available === false) {
    error.textContent = "显卡驱动异常，当前不能下发 OC";
    return;
  }
  const currentCapabilities = gpuOcCapabilities(currentGpu);
  if (target.capabilities.some((field) => !currentCapabilities.includes(field))) {
    error.textContent = "显卡 OC 能力已变化，请刷新设备后重新选择。";
    return;
  }
  let setting;
  try {
    setting = collectDeviceGpuOcSetting(target, form);
  } catch (collectError) {
    error.textContent = collectError.message;
    return;
  }
  const question = `确认向设备“${agent.name}”的 ${target.name}（GPU ${target.index}）下发以下 NVIDIA NVML 设置？\n\nGPU ${setting.index} · ${gpuOcEditSummary(setting)}\n\n超频可能导致不稳定、过热或硬件损坏；Agent 本地限制仍会再次校验。`;
  if (!window.confirm(question)) return;
  setDeviceGpuOcPending(target, true);
  try {
    const response = await api(`/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/apply`, {
      method: "POST",
      body: { vendor: "nvidia", gpu_uuid: target.uuid, setting },
    });
    if (state.deviceGpuOcTarget !== target) return;
    $("#deviceGpuOcDialog").close();
    state.deviceGpuOcTarget = null;
    toast(response.deferred
      ? `GPU ${setting.index} OC 参数已保存；开始或重启挖矿后自动应用`
      : `GPU ${setting.index} OC 任务已进入队列`);
    await refreshAll(true);
  } catch (requestError) {
    if (state.deviceGpuOcTarget === target) error.textContent = requestError.message;
  } finally {
    setDeviceGpuOcPending(target, false);
  }
}

async function applyDeviceGpuOc() {
  return submitDeviceGpuOc(state.deviceGpuOcTarget, $("#deviceGpuOcForm"),
    $("#deviceGpuOcError"), $("#applyDeviceGpuOcButton"));
}

function batchDeviceGpuCandidates(agent) {
  return (Array.isArray(agent?.metrics?.gpus) ? agent.metrics.gpus : [])
    .filter((gpu) => gpuMetricIndex(gpu) != null && gpuMetricIsNvidia(gpu) && gpuMetricUuid(gpu)
      && gpu.driver_available !== false)
    .map((gpu) => {
      const power = gpuPowerRange(gpu);
      return {
      index: gpuMetricIndex(gpu),
      uuid: gpuMetricUuid(gpu),
      name: gpu.name || `GPU ${gpuMetricIndex(gpu)}`,
      hasSavedOc: Boolean(gpuDesiredOcEntry(agent, gpu)),
      capabilities: gpuOcCapabilities(gpu),
      powerMin: power.minimum,
      powerMax: power.maximum,
      fanMin: Number(gpu.fan_min_percent),
      fanMax: Number(gpu.fan_max_percent),
      };
    });
}

function openDeviceGpuOcBatch() {
  const agent = state.selectedAgent;
  const canOperate = state.user?.role === "operator" || state.user?.role === "admin";
  if (!agent || !canOperate) {
    toast("需要运维人员或管理员权限", "error");
    return;
  }
  if (agent.status !== "online") {
    toast("设备离线时不能下发 GPU OC", "error");
    return;
  }
  if (deviceRuntimePreparationReason(agent)) { toast(deviceRuntimePreparationReason(agent), "error"); return; }
  if (!agentVersionAtLeast(agent.agent_version, [0, 6, 16])) {
    toast("批量累计 OC 需要先将该设备升级到 Agent 0.6.16 或更高版本", "error");
    return;
  }
  const gpus = batchDeviceGpuCandidates(agent);
  if (!gpus.length) {
    toast("设备没有上报可安全识别的 NVIDIA GPU UUID", "error");
    return;
  }
  const form = $("#deviceGpuOcBatchForm");
  form.reset();
  state.deviceGpuOcBatchTarget = { agentId: agent.id, gpus };
  $("#deviceGpuOcBatchError").textContent = "";
  $("#deviceGpuOcBatchResults").innerHTML = "";
  $("#deviceGpuOcBatchResults").classList.add("hidden");
  $("#deviceGpuOcBatchTargets").innerHTML = gpus.map((gpu) => `<label class="batch-choice">
    <input type="checkbox" data-device-gpu-oc-batch="${escapeHtml(gpu.uuid)}">
    <span><strong>GPU ${gpu.index} · ${escapeHtml(gpu.name)}</strong><small title="${escapeHtml(gpu.uuid)}">${escapeHtml(gpu.uuid)} · ${gpu.capabilities.length ? `${gpu.capabilities.length} 项运行时能力` : "未确认调节能力"} · ${gpu.hasSavedOc ? "已保存 OC" : "无已保存 OC"}</small></span>
  </label>`).join("");
  $$("[data-device-gpu-oc-batch]", $("#deviceGpuOcBatchTargets")).forEach((input) => input.addEventListener("change", syncDeviceGpuOcBatchSelection));
  syncDeviceGpuOcBatchSelection();
  $("#deviceGpuOcBatchDialog").showModal();
}

function selectedDeviceGpuOcBatchItems() {
  const target = state.deviceGpuOcBatchTarget;
  if (!target) return [];
  return $$("[data-device-gpu-oc-batch]:checked", $("#deviceGpuOcBatchTargets"))
    .map((input) => target.gpus.find((gpu) => gpu.uuid === input.dataset.deviceGpuOcBatch))
    .filter(Boolean);
}

function batchRangeIntersection(selected, minimumKey, maximumKey, fallbackMinimum, fallbackMaximum) {
  const minima = selected.map((gpu) => gpu[minimumKey]).filter(Number.isFinite);
  const maxima = selected.map((gpu) => gpu[maximumKey]).filter(Number.isFinite);
  const minimum = minima.length === selected.length ? Math.max(fallbackMinimum, ...minima) : fallbackMinimum;
  const maximum = maxima.length === selected.length ? Math.min(fallbackMaximum, ...maxima) : fallbackMaximum;
  return minimum <= maximum ? { minimum, maximum } : null;
}

function syncDeviceGpuOcBatchSelection() {
  const selected = selectedDeviceGpuOcBatchItems();
  $("#deviceGpuOcBatchSelectedCount").textContent = `已选择 ${selected.length} 张`;
  const allHaveSavedOc = selected.length > 0 && selected.every((gpu) => gpu.hasSavedOc);
  const intersection = selected.length
    ? gpuOcFields.filter((field) => selected.every((gpu) => gpu.capabilities.includes(field)))
    : [];
  $$("[data-device-gpu-batch-control]", $("#deviceGpuOcBatchForm")).forEach((label) => {
    const field = label.dataset.deviceGpuBatchControl;
    const supported = intersection.includes(field);
    label.classList.toggle("hidden", !supported);
    const input = $("input", label);
    const reset = $(`[data-device-gpu-batch-reset="${field}"]`, $("#deviceGpuOcBatchForm"));
    if (reset) { reset.disabled = !supported; if (!supported) reset.checked = false; }
    input.disabled = !supported || reset?.checked === true;
    if (!supported) input.value = "";
  });
  const powerInput = $('[data-device-gpu-batch-field="power_limit_w"]', $("#deviceGpuOcBatchForm"));
  const powerMinima = selected.map((gpu) => gpu.powerMin).filter(Number.isFinite);
  const powerMaxima = selected.map((gpu) => gpu.powerMax).filter(Number.isFinite);
  powerInput.min = String(Math.max(0, ...powerMinima));
  powerInput.max = powerMaxima.length ? String(Math.min(...powerMaxima)) : "";
  const fanInput = $('[data-device-gpu-batch-field="fan_percent"]', $("#deviceGpuOcBatchForm"));
  const fanRange = batchRangeIntersection(selected, "fanMin", "fanMax", 30, 100);
  if (fanRange) {
    fanInput.min = String(fanRange.minimum);
    fanInput.max = String(fanRange.maximum);
  }
  $("#deviceGpuOcBatchCapabilityHint").textContent = !selected.length
    ? "请先明确勾选至少一张显卡；系统不会默认全选。"
    : intersection.length
      ? `所选 ${selected.length} 张显卡共同确认支持 ${intersection.length} 项参数：${intersection.join("、")}。${allHaveSavedOc ? "" : "清除 OC 仅可用于全部已有保存参数的选择。"}`
      : `所选显卡没有共同确认支持的 OC 参数；不能批量应用新参数。${allHaveSavedOc ? "仍可清除选中卡已保存的 OC。" : "当前选择也不能批量清除 OC。"}`;
  const pending = state.deviceGpuOcBatchTarget?.pending === true;
  $("#applyDeviceGpuOcBatchButton").disabled = pending || !selected.length || !intersection.length;
  $("#clearDeviceGpuOcBatchButton").disabled = pending || !allHaveSavedOc;
}

function validateCurrentBatchGpuTargets(agent, selected) {
  const current = batchDeviceGpuCandidates(agent);
  return selected.every((target) => current.some((gpu) => gpu.index === target.index && gpu.uuid === target.uuid));
}

function collectDeviceGpuOcBatchSetting() {
  const selected = selectedDeviceGpuOcBatchItems();
  if (!selected.length) throw new Error("请明确勾选至少一张显卡。");
  const intersection = gpuOcFields.filter((field) => selected.every((gpu) => gpu.capabilities.includes(field)));
  const setting = {};
  intersection.forEach((field) => {
    const input = $(`[data-device-gpu-batch-field=${field}]`, $("#deviceGpuOcBatchForm"));
    const raw = input.value.trim();
    const reset = $(`[data-device-gpu-batch-reset="${field}"]`, $("#deviceGpuOcBatchForm"));
    if (reset?.checked) setting[field] = null;
    else {
      if (input.validity?.badInput) throw new Error("请输入有效的 OC 数值，或明确选择恢复默认。");
      if (raw !== "") {
        const value = Number(raw);
        if (!gpuOcNumberIsValid(value, field)) throw new Error("请输入有效的 OC 数值；功耗上限须为正的有限数值。");
        setting[field] = value;
      }
    }
  });
  if (!Object.keys(setting).length) throw new Error("至少填写一项参数，或明确勾选要恢复默认的项目。");
  return setting;
}

function batchGpuConfirmationLines(selected) {
  return selected.map((gpu) => `GPU ${gpu.index} · ${gpu.name}\n${gpu.uuid}`).join("\n\n");
}

async function applyDeviceGpuOcBatch() {
  const target = state.deviceGpuOcBatchTarget;
  if (target?.pending) return;
  const agent = target ? state.agents.find((item) => item.id === target.agentId) : null;
  if (deviceRuntimePreparationReason(agent)) { $("#deviceGpuOcBatchError").textContent = deviceRuntimePreparationReason(agent); return; }
  const selected = selectedDeviceGpuOcBatchItems();
  const error = $("#deviceGpuOcBatchError");
  error.textContent = "";
  if (!target || !agent || agent.status !== "online") {
    error.textContent = "设备已改变或离线，请关闭弹窗后重试。";
    return;
  }
  if (!validateCurrentBatchGpuTargets(agent, selected)) {
    error.textContent = "至少一张 GPU 的 Index、UUID 或驱动可用状态已变化，请关闭弹窗、刷新设备后重新选择。";
    return;
  }
  let setting;
  try {
    setting = collectDeviceGpuOcBatchSetting();
  } catch (collectError) {
    error.textContent = collectError.message;
    return;
  }
  if (!window.confirm(`确认向设备“${agent.name}”明确选中的 ${selected.length} 张显卡应用同一组参数？\n\n${batchGpuConfirmationLines(selected)}\n\n参数：${gpuOcEditSummary(setting)}\n\n未勾选显卡绝不会改变。`)) return;
  setDeviceGpuOcPending(target, true);
  try {
    const response = await api(`/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/batch-apply`, {
      method: "POST",
      body: { vendor: "nvidia", gpu_uuids: selected.map((gpu) => gpu.uuid), setting },
    });
    if (state.deviceGpuOcBatchTarget !== target) return;
    renderBatchOperationResults(
      $("#deviceGpuOcBatchResults"),
      selected.map((gpu) => ({ id: gpu.uuid, label: `GPU ${gpu.index} · ${gpu.name}`, detail: gpu.uuid })),
      response,
      ["gpu_uuid", "uuid"],
    );
    toast(response.deferred
      ? `已保存 ${selected.length} 张显卡的 OC 参数；开始或重启挖矿后自动应用`
      : `批量 OC 请求已提交，控制端已逐卡返回 ${selected.length} 项结果`);
    await refreshAll(true);
  } catch (requestError) {
    if (state.deviceGpuOcBatchTarget === target) error.textContent = requestError.message;
  } finally {
    setDeviceGpuOcPending(target, false);
  }
}

async function clearDeviceGpuOcBatch() {
  const target = state.deviceGpuOcBatchTarget;
  if (target?.pending) return;
  const agent = target ? state.agents.find((item) => item.id === target.agentId) : null;
  if (deviceRuntimePreparationReason(agent)) { $("#deviceGpuOcBatchError").textContent = deviceRuntimePreparationReason(agent); return; }
  const selected = selectedDeviceGpuOcBatchItems();
  const error = $("#deviceGpuOcBatchError");
  error.textContent = "";
  if (!target || !agent || agent.status !== "online" || !selected.length) {
    error.textContent = "设备已改变、离线或尚未选择显卡，请关闭弹窗后重试。";
    return;
  }
  if (!selected.every((gpu) => gpu.hasSavedOc)) {
    error.textContent = "批量清除只适用于全部已有保存 OC 的显卡；请取消未保存 OC 的选择。";
    return;
  }
  if (!validateCurrentBatchGpuTargets(agent, selected)) {
    error.textContent = "至少一张 GPU 的 Index、UUID 或驱动可用状态已变化，请关闭弹窗、刷新设备后重新选择。";
    return;
  }
  if (!window.confirm(`确认只清除设备“${agent.name}”明确选中的 ${selected.length} 张显卡 OC？\n\n${batchGpuConfirmationLines(selected)}\n\n控制端保存参数与硬件调节将只针对这些卡清除；未勾选显卡绝不会改变。`)) return;
  setDeviceGpuOcPending(target, true);
  try {
    const response = await api(`/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/batch-clear`, {
      method: "POST",
      body: { vendor: "nvidia", gpu_uuids: selected.map((gpu) => gpu.uuid) },
    });
    if (state.deviceGpuOcBatchTarget !== target) return;
    renderBatchOperationResults(
      $("#deviceGpuOcBatchResults"),
      selected.map((gpu) => ({ id: gpu.uuid, label: `GPU ${gpu.index} · ${gpu.name}`, detail: gpu.uuid })),
      response,
      ["gpu_uuid", "uuid"],
    );
    toast(`批量清除请求已提交，控制端已逐卡返回 ${selected.length} 项结果`);
    await refreshAll(true);
  } catch (requestError) {
    if (state.deviceGpuOcBatchTarget === target) error.textContent = requestError.message;
  } finally {
    setDeviceGpuOcPending(target, false);
  }
}
function showPage(page) {
  if (["access", "agentReleases"].includes(page) && state.user?.role !== "admin") page = "dashboard";
  const changed = state.page !== page;
  state.page = page;
  const titles = {
    dashboard: ["CONTROL CENTER", "设备总览"], devices: ["FLEET", "设备管理"],
    wallets: ["PAYOUT", "钱包管理"], pools: ["POOLS", "矿池"], miners: ["MINER PACKAGES", "矿工软件库"], flights: ["FLIGHT SHEETS", "飞行表"], workload: ["WORKLOAD", "工作量"],
    agentReleases: ["AGENT RELEASES", "被控端版本"],
    access: ["SETTINGS", "系统设置"],
  };
  $$(".page").forEach((element) => element.classList.toggle("active", element.id === `${page}Page`));
  $$(".nav-item").forEach((element) => {
    const active = element.dataset.page === page;
    element.classList.toggle("active", active);
    if (!active) {
      element.removeAttribute("aria-current");
      return;
    }
    element.setAttribute("aria-current", "page");
    const nav = element.parentElement;
    if (changed && nav && nav.scrollWidth > nav.clientWidth) {
      // A page shortcut can activate a button outside the mobile nav strip.
      // Scroll the strip alone so a horizontal adjustment cannot move the page.
      const item = element.getBoundingClientRect(), strip = nav.getBoundingClientRect();
      if (item.left < strip.left) nav.scrollLeft += item.left - strip.left;
      else if (item.right > strip.right) nav.scrollLeft += item.right - strip.right;
    }
  });
  $("#pageKicker").textContent = titles[page][0];
  $("#pageTitle").textContent = titles[page][1];
  if (changed && document.scrollingElement) document.scrollingElement.scrollTop = 0;
  if (workloadRefreshDue()) refreshWorkloadSummary();
}

function captureDrawerState() {
  const content = $("#drawerContent");
  if (!content) return null;
  const active = document.activeElement;
  const fields = {};
  for (const id of ["agentDisplayName"]) {
    const element = $(`#${id}`, content);
    if (!element || element.dataset.dirty !== "true") continue;
    fields[id] = element.type === "checkbox" ? { checked: element.checked } : { value: element.value };
  }
  return {
    agentId: content.dataset.agentId,
    focusDisclosure: active?.tagName === "SUMMARY" ? active.parentElement?.dataset.disclosureKey || "" : "",
    scrollTop: content.scrollTop,
    gpuTableLeft: $(".device-detail-gpus > .table-wrap", content)?.scrollLeft || 0,
    focusGpuUuid: active && content.contains(active) ? active.dataset?.gpuUuid : null,
    fields,
    focusId: active && content.contains(active) ? active.id : "",
    selectionStart: typeof active?.selectionStart === "number" ? active.selectionStart : null,
    selectionEnd: typeof active?.selectionEnd === "number" ? active.selectionEnd : null,
  };
}

function restoreDrawerState(snapshot) {
  const content = $("#drawerContent");
  if (!snapshot || !content || content.dataset.agentId !== snapshot.agentId) return;
  for (const [id, saved] of Object.entries(snapshot.fields)) {
    const element = $(`#${id}`, content);
    if (!element) continue;
    if (element.type === "checkbox") element.checked = saved.checked;
    else element.value = saved.value;
    element.dataset.dirty = "true";
  }
  content.scrollTop = snapshot.scrollTop;
  const gpuTable = $(".device-detail-gpus > .table-wrap", content);
  if (gpuTable) gpuTable.scrollLeft = snapshot.gpuTableLeft;
  let focus = snapshot.focusId ? $(`#${snapshot.focusId}`, content) : null;
  if (snapshot.focusGpuUuid) {
    focus = $$("button[data-device-gpu-oc]", content).find(button => button.dataset.gpuUuid === snapshot.focusGpuUuid);
  }
  if (snapshot.focusDisclosure) {
    focus = $$('details[data-disclosure-key]', content)
      .find((details) => details.dataset.disclosureKey === snapshot.focusDisclosure)?.querySelector("summary");
  }
  if (focus && !focus.disabled) {
    focus.focus({ preventScroll: true });
    if (snapshot.selectionStart != null && typeof focus.setSelectionRange === "function") {
      focus.setSelectionRange(snapshot.selectionStart, snapshot.selectionEnd ?? snapshot.selectionStart);
    }
  }
}

function refreshSelectedAgentDrawer(preserveInputs = true) {
  const drawer = $("#deviceDrawer");
  const agent = state.selectedAgent;
  if (!agent || !drawer?.classList.contains("open")) return;
  $("#drawerName").textContent = "设备详情";
  renderDrawer(agent, preserveInputs);
}

async function openAgent(agentId) {
  const agent = state.agents.find((item) => item.id === agentId);
  if (!agent) return;
  if ($("#minerCacheDialog")?.open) closeMinerCacheCleanup();
  state.selectedAgent = agent;
  state.deviceDetailTab = Array.isArray(agent.metrics?.gpus) && agent.metrics.gpus.length ? "gpus" : "overview";
  state.deviceLogRequestId += 1;
  state.deviceLog = null;
  state.deviceHashrateRequestId += 1;
  state.deviceHashrateHistory = null;
  state.selectedAgentTelemetry = [];
  state.selectedAgentTelemetryId = agent.id;
  const requestId = ++state.deviceTelemetryRequestId;
  const authEpoch = state.authEpoch;
  const stillCurrent = () => requestId === state.deviceTelemetryRequestId
    && authEpoch === state.authEpoch && state.selectedAgent?.id === agent.id;
  $("#drawerName").textContent = agent.name;
  renderDrawer(agent, false);
  $("#drawerContent").scrollTop = 0;
  $("#backdrop").classList.remove("hidden");
  $("#deviceDrawer").classList.add("open");
  $("#deviceDrawer").setAttribute("aria-hidden", "false");
  try {
    const telemetry = await api(`/api/agents/${encodeURIComponent(agent.id)}/telemetry?limit=60`);
    if (stillCurrent()) {
      state.selectedAgentTelemetry = Array.isArray(telemetry) ? telemetry : [];
      state.selectedAgentTelemetryId = agent.id;
      drawTelemetry(state.selectedAgentTelemetry);
    }
  } catch (error) {
    if (stillCurrent()) toast(error.message, "error");
  }
}

function miningStateLabel(value) {
  return {
    running: "进程运行 · 上报有算力",
    active: "进程运行 · 上报有算力",
    degraded: "部分矿工运行",
    starting: "正在启动",
    stopping: "正在停止",
    stopped: "已停止",
    inactive: "已停止",
    failed: "运行失败",
    error: "运行异常",
    stale: "状态过期/进程已不存在",
    offline: "设备离线/状态未确认",
    not_configured: "未配置",
    unconfirmed: "状态异常/未确认",
    unknown: "状态未知",
    running_no_stats: "进程运行 · 统计待确认",
    running_attention: "进程运行 · 状态待确认",
    running_zero_hashrate: "进程运行 · 上报暂无算力",
    running_partial_hashrate: "部分矿工算力待确认",
  }[String(value || "unknown").toLowerCase()] || "状态未知";
}

function miningStateClass(value) {
  const stateValue = String(value || "unknown").toLowerCase();
  if (["running", "active"].includes(stateValue)) return "";
  if (["degraded", "starting", "stopping", "failed", "error", "stale", "unconfirmed", "running_no_stats", "running_attention", "running_zero_hashrate", "running_partial_hashrate"].includes(stateValue)) return "warning";
  return "disabled";
}

function miningDisplayState(status, agent = null) {
  const rawState = String(status?.state || status?.service_state || "unknown").toLowerCase();
  const runningRoles = Array.isArray(status?.running_roles)
    ? status.running_roles.filter((role) => ["primary", "secondary"].includes(String(role)))
    : [];
  const degraded = status?.degraded === true && runningRoles.length > 0;
  if (!["running", "active"].includes(rawState) && !degraded) return rawState;
  const processError = String(status?.process_error || "").trim();
  const reportedError = String(status?.error || "").trim();
  if (!degraded && (status?.process_verified === false || processError)) return "stale";
  if (!degraded && reportedError && !miningProcessIsConfirmed(status, agent)) return "unconfirmed";
  const checkedAt = Date.parse(status?.checked_at);
  const statsAt = Date.parse(status?.stats_updated_at);
  const bootAt = Date.parse(agent?.metrics?.boot_time);
  const controllerHeartbeatAt = Date.parse(agent?.last_seen);
  // A current controller heartbeat cannot make a cached miner sample current.
  // Require the heartbeat itself and the sample collected for that heartbeat
  // to be recent and mutually close; this fails closed after a reboot or when
  // the Agent keeps heartbeating with an old miner snapshot.
  const controllerHeartbeatIsFresh = agent?.status === "online"
    && Number.isFinite(controllerHeartbeatAt)
    && Math.abs(Date.now() - controllerHeartbeatAt) <= 120000;
  const statsAreFresh = Number.isFinite(checkedAt)
    && Number.isFinite(statsAt)
    && controllerHeartbeatIsFresh
    && Math.abs(controllerHeartbeatAt - checkedAt) <= 60000
    && statsAt <= checkedAt + 30000
    && checkedAt - statsAt <= 30000
    && (!Number.isFinite(bootAt) || statsAt >= bootAt);
  const declaredStats = String(status?.stats_state || "");
  const processConfirmed = miningProcessIsConfirmed(status, agent);
  if (!degraded && status?.process_verified === true && !processConfirmed) return "unconfirmed";
  if (!degraded && processConfirmed) {
    if (reportedError && (!declaredStats || declaredStats === "fresh")) return "running_attention";
    if (!statsAreFresh || (declaredStats && declaredStats !== "fresh")) return "running_no_stats";
  }
  if (declaredStats && declaredStats !== "fresh") return "unconfirmed";
  if (!statsAreFresh) return "unconfirmed";
  if (degraded) return "degraded";
  const details = miningHashrateDetails(status);
  const rates = details.map((detail) => miningHashrateDetailIsFresh(status, detail, agent)
    ? hashrateToHps(miningHashrateObservedValue(detail.hashrate), detail.hashrate_unit || status.hashrate_unit) : null);
  if (!rates.length || rates.every((rate) => rate === null)) return "running_no_stats";
  if (rates.every((rate) => rate === 0)) return "running_zero_hashrate";
  if (rates.some((rate) => rate === null || rate === 0)) return "running_partial_hashrate";
  return rawState;
}

const HASHRATE_UNITS = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s", "EH/s"];

function formatHashrateAmount(value, unit = "H/s") {
  if (!hasHashrateAmount(value)) return "—";
  let amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return "—";
  const normalizedUnit = String(unit || "H/s").replaceAll(" ", "").toLowerCase();
  let unitIndex = HASHRATE_UNITS.findIndex((candidate) => candidate.toLowerCase() === normalizedUnit);
  if (unitIndex < 0) {
    return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(amount)} ${unit || "H/s"}`;
  }
  while (
    unitIndex < HASHRATE_UNITS.length - 1
    && (amount >= 1000 || Number(amount.toFixed(2)) >= 1000)
  ) {
    amount /= 1000;
    unitIndex += 1;
  }
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(amount)} ${HASHRATE_UNITS[unitIndex]}`;
}

function formatHashrate(value, unit = "H/s") {
  if (value == null || value === "") return "—";
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const parts = [
      ["主挖", value.primary],
      ["副挖", value.secondary],
    ].filter(([, amount]) => amount != null && Number.isFinite(Number(amount)));
    if (parts.length) return parts.map(([label, amount]) => `${label} ${formatHashrateAmount(amount, unit)}`).join(" · ");
    const total = value.total ?? value.total_hashrate;
    return total != null ? formatHashrateAmount(total, unit) : "—";
  }
  return formatHashrateAmount(value, unit);
}

function miningHashrateMarkup(status, agent = null) {
  const details = miningHashrateDetails(status);
  if (!details.length) {
    return `<strong>${escapeHtml(formatHashrate(status.hashrate, status.hashrate_unit))}</strong>`;
  }
  const rows = details.map((detail, index) => {
    const fresh = miningHashrateDetailIsFresh(status, detail, agent);
    const rate = fresh ? miningHashrateObservedValue(detail.hashrate) : null;
    const sourceRole = miningHashrateRole(detail, index);
    const role = sourceRole === "primary" ? "主挖" : sourceRole === "secondary" ? "副挖" : "算法";
    const identity = [detail.coin, detail.algorithm].filter(Boolean).join(" · ") || role;
    const pool = detail.pool_url || "矿池未上报";
    const shares = fresh && detail.shares && typeof detail.shares === "object" ? detail.shares : null;
    const proofs = fresh && detail.proofs && typeof detail.proofs === "object" ? detail.proofs : null;
    const shareLabel = shares
      ? (Object.prototype.hasOwnProperty.call(shares, "stale")
        ? `Pool A/R(St) ${shares.accepted ?? "—"}/${shares.rejected ?? "—"}(${shares.stale ?? "—"})`
        : `Share 接受 ${shares.accepted ?? "—"} · 拒绝 ${shares.rejected ?? "—"}`)
      : "Share 尚未上报";
    const proofLabel = proofs
      ? `Proof S/R/D ${proofs.s ?? "—"}/${proofs.r ?? "—"}/${proofs.d ?? "—"}`
      : "";
    const counters = [proofLabel, shareLabel].filter(Boolean).join(" · ");
    return `<div class="mining-hashrate-row">
      <div class="mining-hashrate-copy"><b>${escapeHtml(identity)}</b><small>${escapeHtml(role)} · ${escapeHtml(pool)}</small><small>${escapeHtml(counters)}</small></div>
      <strong>${escapeHtml(rate !== null ? formatHashrateAmount(rate, detail.hashrate_unit || status.hashrate_unit) : "--")}</strong>
    </div>`;
  }).join("");
  return `<div class="mining-hashrate-list">${rows}</div>`;
}

function miningShareLabel(status, confirmedRunning) {
  if (!confirmedRunning) return "—";
  const counterLabel = (shares) => {
    if (!shares || typeof shares !== "object") return "";
    const values = [
      `接受 ${shares.accepted ?? "—"}`,
      `拒绝 ${shares.rejected ?? "—"}`,
    ];
    if (Object.prototype.hasOwnProperty.call(shares, "stale")) {
      values.push(`过期 ${shares.stale ?? "—"}`);
    }
    return values.join(" · ");
  };
  const aggregate = counterLabel(status?.shares);
  if (aggregate) return aggregate;

  const details = Array.isArray(status?.hashrate_details)
    ? status.hashrate_details.filter((detail) => detail && typeof detail === "object")
    : [];
  const seen = new Set();
  const roleCounters = [];
  for (const detail of details) {
    const role = detail.role === "primary"
      ? "主挖"
      : detail.role === "secondary" ? "副挖" : "";
    const counters = counterLabel(detail.shares);
    if (!role || !counters || seen.has(role)) continue;
    seen.add(role);
    roleCounters.push(`${role} ${counters}`);
  }
  return roleCounters.length ? roleCounters.join("；") : "—";
}

function miningProcessLabel(process, minerType = "") {
  if (!process || typeof process !== "object") return "未配置";
  const profile = String(process.miner_profile || process.profile || "未知模板").trim();
  const minerPackage = process.miner_package;
  const packageLabel = minerPackage && typeof minerPackage === "object"
    ? [minerPackage.name, minerPackage.version].filter(Boolean).join(" ")
    : "设备已有软件";
  if (process.miner_type === "hiveos" || minerType === "hiveos" || profile === "hiveos-custom.v1") {
    return minerPackage && typeof minerPackage === "object" ? packageLabel || "软件包名称未上报" : "软件包名称未上报";
  }
  if (profile.toLowerCase() === "default") return packageLabel;
  return `${profile} · ${packageLabel}`;
}

function agentSupervisionLabel(agent, inventory) {
  if (agent.status !== "online") return "设备离线 · 状态未确认";
  const supervision = inventory.agent_supervision;
  if (!supervision || typeof supervision !== "object") {
    return "未上报监管信息";
  }
  const serviceName = supervision.service_name || "systemd Watchdog";
  const state = String(supervision.state || "unavailable").toLowerCase();
  const labels = {
    ready: "运行中",
    repair_pending: "正在配置",
    misconfigured: "配置异常",
    unavailable: "不可用",
    unsupported: "当前系统不支持",
    disabled: "未启用",
  };
  return `${serviceName} · ${labels[state] || "状态未知"}`;
}

function miningStatusMarkup(agent) {
  const inventory = agent.inventory && typeof agent.inventory === "object" ? agent.inventory : {};
  const status = miningStatusForAgent(agent);
  if (!status || typeof status !== "object") {
    return `<div class="mining-status-card unavailable"><div><span class="status-pill disabled">尚未上报</span><p>当前 Agent 未提供挖矿状态。升级并启动新版 Agent 后，这里会显示服务、飞行表与算力数据。</p></div><button class="btn btn-secondary btn-small" type="button" data-refresh-mining>刷新状态</button></div>`;
  }
  const isOnline = agent.status === "online";
  const stateValue = !isOnline
    ? "offline"
    : status.configured === false ? "not_configured" : miningDisplayState(status, agent);
  const stateClass = miningStateClass(stateValue);
  const confirmedRunning = isOnline && ["running", "active", "degraded", "running_zero_hashrate", "running_partial_hashrate"].includes(stateValue);
  const supervision = agentSupervisionLabel(agent, inventory);
  const flight = status.flight_sheet_name || status.flight_sheet_id || "未应用飞行表";
  const shares = miningShareLabel(status, confirmedRunning);
  const runtimeLabel = ["running_no_stats", "running_attention"].includes(stateValue)
    ? miningControlPresentation(agent, status, stateValue).label : miningStateLabel(stateValue);
  const hashrateMarkup = confirmedRunning
    ? miningHashrateMarkup(status, agent)
    : '<span class="device-hashrate-empty">—</span>';
  const updatedAt = status.stats_updated_at || status.checked_at || status.applied_at;
  const statusError = [...new Set([
    !isOnline ? "设备当前离线；下列配置来自最后一次心跳，运行状态与算力不再可信。" : null,
    ...miningStatusErrorMessages(status),
  ].filter(Boolean).map(String))].join("；");
  return `<div class="mining-status-card">
    <div class="mining-status-head"><div><span class="status-pill ${stateClass}">${escapeHtml(runtimeLabel)}</span><strong>${escapeHtml(flight)}</strong></div><button class="btn btn-secondary btn-small" type="button" data-refresh-mining>刷新状态</button></div>
    <div class="mining-detail-grid">
      <div><span>监管服务</span><strong>${escapeHtml(supervision)}</strong></div>
      <div><span>Worker</span><strong>${escapeHtml(status.worker_name || "—")}</strong></div>
      <div class="mining-hashrate-panel"><span>分项算力</span>${hashrateMarkup}</div>
      <div><span>Share</span><strong>${escapeHtml(shares)}</strong></div>
      <div><span>统计状态</span><strong>${escapeHtml(miningStatisticsLabel(status, agent))}</strong></div>
      <div><span>主挖</span><strong>${escapeHtml(miningProcessLabel(status.primary, status.miner_type))}</strong></div>
      <div><span>副挖</span><strong>${escapeHtml(status.secondary ? miningProcessLabel(status.secondary) : "未启用")}</strong></div>
    </div>
    ${updatedAt ? `<small class="mining-updated">状态更新时间：${escapeHtml(formatTime(updatedAt, false))}</small>` : ""}
    ${statusError ? `<div class="mining-error">${escapeHtml(statusError)}</div>` : ""}
  </div>`;
}

async function refreshAgentStatus() {
  const agentId = state.selectedAgent?.id;
  if (!agentId) return;
  const authEpoch = state.authEpoch;
  const stillCurrent = () => state.authEpoch === authEpoch && state.selectedAgent?.id === agentId;
  const button = $("[data-refresh-mining]", $("#drawerContent"));
  if (button) button.disabled = true;
  try {
    const updated = await api(`/api/agents/${encodeURIComponent(agentId)}`);
    if (!stillCurrent()) return;
    state.agents = state.agents.map((agent) => agent.id === agentId ? updated : agent);
    state.selectedAgent = updated;
    refreshSelectedAgentDrawer(true);
    renderDevices();
    toast("挖矿状态已刷新");
  } catch (error) {
    if (!stillCurrent()) return;
    toast(error.message, "error");
    if (button) button.disabled = false;
  }
}

async function saveAgentDetails(event) {
  event.preventDefault();
  const agent = state.selectedAgent;
  if (!agent || state.user?.role !== "admin") return;
  const authEpoch = state.authEpoch;
  const stillCurrent = () => state.authEpoch === authEpoch && state.selectedAgent?.id === agent.id;
  const name = $("#agentDisplayName").value.trim();
  if (!name) return;
  const button = $("#saveAgentDetailsButton");
  button.disabled = true;
  try {
    const updated = await api(`/api/agents/${encodeURIComponent(agent.id)}`, { method: "PATCH", body: { name } });
    if (!stillCurrent()) return;
    state.agents = state.agents.map((item) => item.id === agent.id ? updated : item);
    state.selectedAgent = updated;
    const currentName = $("#agentDisplayName")?.value;
    refreshSelectedAgentDrawer(currentName !== undefined && currentName.trim() !== name);
    renderDevices();
    toast("设备名称已保存");
  } catch (error) {
    if (!stillCurrent()) return;
    toast(error.message, "error");
    button.disabled = false;
  }
}

function agentVersionAtLeast(version, minimum) {
  const match = String(version || "").match(/^(\d+)\.(\d+)\.(\d+)(?:\D|$)/);
  if (!match) return false;
  const current = match.slice(1).map(Number);
  for (let index = 0; index < minimum.length; index += 1) {
    if (current[index] !== minimum[index]) return current[index] > minimum[index];
  }
  return true;
}

function agentSupportsMinerLogs(version) {
  return agentVersionAtLeast(version, [0, 5, 1]);
}

function agentSupportsMiningControl(version) {
  return agentVersionAtLeast(version, [0, 5, 2]);
}

async function cancelMinerLogCommand(commandId) {
  if (!commandId) return;
  try {
    await api(`/api/commands/${encodeURIComponent(commandId)}/cancel`, { method: "POST" });
  } catch (_) {
    // The Agent may have already claimed or finished the command.
  }
}

function minerCacheContextCurrent(run, token) {
  return state.minerCacheCleanup === run
    && state.minerCacheCleanupToken === token
    && state.authEpoch === run.authEpoch
    && state.selectedAgent?.id === run.agentId
    && $("#deviceDrawer")?.classList.contains("open")
    && $("#minerCacheDialog")?.open;
}

function minerCacheReasonText(reason) {
  const known = {
    cache_root_unsafe: "矿工缓存目录无法安全确认",
    unsafe_cache_root: "矿工缓存目录不满足安全清理条件",
    unsafe_cache_lock: "矿工缓存锁不安全，未执行清理",
    cache_busy: "矿工缓存正在被其他任务使用，请稍后重试",
    config_disabled: "设备未启用受管挖矿缓存",
    flight_sheet_unavailable: "当前飞行表无法读取，已停止扫描",
    flight_sheet_invalid: "当前飞行表结构无法确认，已停止扫描",
    runtime_unavailable: "矿工运行记录无法读取，已停止扫描",
    runtime_unsafe: "矿工运行状态无法安全确认",
    scan_unavailable: "设备无法完成安全扫描",
    scan_limit_exceeded: "缓存条目超过安全扫描上限，未生成清理计划",
    scan_timeout: "安全扫描超时，未生成清理计划",
    session_unclassified: "旧运行会话中有无法确认的条目，未生成清理计划",
    cleanup_unavailable: "设备无法执行安全清理",
    cleanup_timeout: "清理超时，后续文件已保留",
    scan_changed: "设备状态在扫描后变化，请重新扫描",
    current_flight_unknown: "当前飞行表的矿工引用尚未确认",
    active_process_unknown: "运行中的矿工引用尚未确认",
    installation_in_progress: "正在安装矿工软件，请稍后重试",
    cleanup_residue_retained: "旧清理残留已保留，不影响其他已确认旧包的清理",
    stale_scan: "扫描快照已变化，请重新扫描",
    reference_changed: "矿工引用在扫描后发生变化",
    directory_changed: "缓存目录在扫描后发生变化",
    in_use: "软件仍在使用，已保留",
    unsafe_entry: "目录安全性无法确认，已保留",
    permission_denied: "设备权限不足，未删除",
    deletion_failed: "删除未完成，已保留",
    unsafe_link: "目录含链接，已保留",
    unsafe_reparse: "目录含重解析点，已保留",
    invalid_marker: "包标记不完整，已保留",
    content_changed: "文件内容已变化，已保留",
    unknown_entry: "目录内容无法分类，已保留",
    unreadable: "无法读取，已保留",
    external_reference: "存在外部引用，已保留",
    unsupported_layout: "缓存布局不支持安全清理",
    current_primary: "当前主挖使用，已保留",
    current_secondary: "当前副挖使用，已保留",
    active_process: "正在运行，已保留",
    active_session: "运行会话使用，已保留",
    unknown_reference: "引用无法确认，已保留",
  };
  return known[String(reason || "")] || (reason ? `设备拒绝清理（${String(reason).slice(0, 64)}）` : "设备未提供可确认的原因");
}

function minerCacheKindLabel(kind) {
  return kind === "hiveos_session_payload" ? "旧运行副本" : "旧软件包";
}

function minerCacheByteText(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? formatBytes(value) : "未上报";
}

function renderMinerCacheCleanup() {
  const run = state.minerCacheCleanup;
  if (!run || !$("#minerCacheDialog")?.open) return;
  const scan = run.scan;
  const result = run.result;
  const busy = ["scanning", "cleaning"].includes(run.phase);
  const candidates = Array.isArray(scan?.candidates) ? scan.candidates : [];
  const protectedItems = Array.isArray(scan?.protected) ? scan.protected : [];
  const unclassified = Array.isArray(scan?.unclassified) ? scan.unclassified : [];
  const titles = {
    scanning: "正在扫描旧矿工缓存",
    ready: scan?.can_cleanup === false ? "当前无法安全清理" : candidates.length ? "扫描完成，等待确认" : "没有可安全清理的旧矿工文件",
    cleaning: "正在清理扫描确认的旧矿工文件",
    succeeded: "清理完成",
    stale: "扫描已过期，请重新扫描",
    partial: "仅完成部分清理",
    error: "本次清理未完成",
  };
  const details = run.phase === "scanning" ? "扫描只检查 Agent 管理的矿工缓存，不会停矿或删除文件。"
    : run.phase === "cleaning" ? "Agent 正在重新核验扫描指纹与当前矿工引用。"
    : run.phase === "ready" && scan?.can_cleanup === false ? minerCacheReasonText(scan.reason)
    : run.phase === "ready" ? "仅下方可清理列表会作为本次清理候选；当前使用的软件始终保留。"
    : run.message || "请查看下方结果。";
  const summary = $("#minerCacheSummary");
  summary.className = `miner-cache-summary ${["stale", "partial", "error"].includes(run.phase) || scan?.can_cleanup === false ? "warning" : ""}`;
  summary.innerHTML = `${busy ? '<span class="spinner-dot"></span>' : ""}<div><strong>${escapeHtml(titles[run.phase] || "准备扫描")}</strong><small>${escapeHtml(details)}</small></div>`;
  const scanFree = scan?.disk?.free_bytes;
  const actualDelta = result?.disk?.free_bytes_delta;
  const beforeFree = result?.disk?.free_bytes_before ?? scanFree;
  const afterFree = result?.disk?.free_bytes_after;
  const deltaText = typeof actualDelta === "number" && Number.isFinite(actualDelta)
    ? actualDelta < 0 ? `包缓存文件系统实测可用空间减少 ${formatBytes(-actualDelta)}` : `包缓存文件系统实测可用空间增加 ${formatBytes(actualDelta)}`
    : "包缓存文件系统未取得可靠的空闲空间变化";
  const resultFilesystemsMeasured = Array.isArray(result?.disk?.filesystems);
  const perFilesystem = resultFilesystemsMeasured ? result.disk.filesystems
    : Array.isArray(scan?.disk?.filesystems) ? scan.disk.filesystems : [];
  const sessionFilesystems = perFilesystem.filter((item) => item?.scope === "hiveos_session_payload");
  const sessionDiskRows = sessionFilesystems.map((item, index) => {
    const priorOnly = Boolean(result) && !resultFilesystemsMeasured;
    const measuredDelta = item.free_bytes_delta;
    const detail = priorOnly ? "任务未取得该文件系统清理后的实测值"
      : result ? typeof measuredDelta === "number" && Number.isFinite(measuredDelta)
        ? measuredDelta < 0 ? `该文件系统实测可用减少 ${formatBytes(-measuredDelta)}` : `该文件系统实测可用增加 ${formatBytes(measuredDelta)}`
        : "该文件系统未取得可靠空闲变化" : "扫描时该文件系统可用空间";
    const label = priorOnly ? "扫描时可用 · 清理后未测" : result ? "清理后可用" : "扫描时可用";
    return `<div class="miner-cache-filesystem"><span>旧运行副本文件系统 ${index + 1} · ${label}</span><strong>${escapeHtml(minerCacheByteText(priorOnly ? item.free_bytes : result ? item.free_bytes_after : item.free_bytes))}</strong><small>${escapeHtml(detail)}；未绑定概览根分区</small></div>`;
  }).join("");
  $("#minerCacheDisk").innerHTML = `<div><span>软件包缓存文件系统 · 扫描时可用</span><strong>${escapeHtml(minerCacheByteText(beforeFree))}</strong><small>清理任务实测；不代表概览根分区/C:</small></div><div><span>${result ? "软件包缓存文件系统 · 清理后可用" : "可清理文件体积（估算）"}</span><strong>${escapeHtml(result ? minerCacheByteText(afterFree) : scan ? formatBytes(scan.candidate_logical_bytes) : "—")}</strong><small>${escapeHtml(result ? deltaText : "跨候选文件长度合计，不等于实际可用空间")}</small></div>${sessionDiskRows}`;
  const deletedIds = new Set(Array.isArray(result?.deleted) ? result.deleted.map((item) => item.candidate_id) : []);
  const skippedIds = new Set(Array.isArray(result?.skipped) ? result.skipped.map((item) => item.candidate_id) : []);
  const failedIds = new Set(Array.isArray(result?.failed) ? result.failed.map((item) => item.candidate_id) : []);
  $("#minerCacheCandidates").innerHTML = candidates.length
    ? candidates.map((item) => {
      const outcome = deletedIds.has(item.candidate_id) ? "已清理"
        : failedIds.has(item.candidate_id) ? "清理失败"
        : skippedIds.has(item.candidate_id) ? "已保留"
        : result ? "未清理" : "等待确认";
      const outcomeClass = deletedIds.has(item.candidate_id) ? "is-cleaned"
        : failedIds.has(item.candidate_id) ? "is-failed" : "is-kept";
      return `<div class="miner-cache-item"><strong>${escapeHtml(item.name)} · ${escapeHtml(item.version || "版本未知")}</strong><span>${escapeHtml(formatBytes(item.logical_bytes))}</span><small>${escapeHtml(minerCacheKindLabel(item.kind))}${item.package_id ? ` · 包 ${escapeHtml(item.package_id)}` : ""} · ${Number(item.file_count)} 个文件</small><small class="miner-cache-candidate-state ${outcomeClass}">${escapeHtml(outcome)}</small></div>`;
    }).join("")
    : '<div class="miner-cache-empty">没有可安全清理的旧矿工文件</div>';
  $("#minerCacheProtected").innerHTML = protectedItems.length
    ? protectedItems.map((item) => `<div class="miner-cache-item"><strong>${escapeHtml(item.package_id || item.entry_id)}</strong><span>${escapeHtml(item.version || "版本未知")}</span><small>${escapeHtml(minerCacheKindLabel(item.kind))} · ${escapeHtml(minerCacheReasonText(item.reason))}</small></div>`).join("")
    : '<div class="miner-cache-empty">本次扫描没有列出当前受保护包</div>';
  $("#minerCacheUnclassified").innerHTML = unclassified.length
    ? unclassified.map((item) => `<div class="miner-cache-item"><strong>${escapeHtml(item.entry_id || "未命名会话条目")}</strong><small>${escapeHtml(minerCacheKindLabel(item.kind))} · ${escapeHtml(minerCacheReasonText(item.reason))}</small></div>`).join("")
    : '<div class="miner-cache-empty">没有未归类条目</div>';
  const resultBox = $("#minerCacheResult");
  resultBox.className = `miner-cache-result ${["stale", "partial", "error"].includes(run.phase) ? "warning" : ""}`;
  if (result) {
    const deleted = Array.isArray(result.deleted) ? result.deleted : [];
    const skipped = Array.isArray(result.skipped) ? result.skipped : [];
    const failed = Array.isArray(result.failed) ? result.failed : [];
    const candidateById = new Map(candidates.map((item) => [item.candidate_id, item]));
    resultBox.innerHTML = `<strong>${escapeHtml(run.phase === "succeeded" ? "本次旧矿工文件清理完成" : "本次清理未全部完成")}</strong><span>完整删除 ${deleted.length} 项旧矿工文件；删除文件体积 ${escapeHtml(formatBytes(result.logical_bytes_removed))}（逻辑大小，不代表空闲空间增量）。</span>${skipped.map((item) => `<span>保留 ${escapeHtml(minerCacheKindLabel(item.kind))} ${escapeHtml(candidateById.get(item.candidate_id)?.name || "旧矿工文件")}：${escapeHtml(minerCacheReasonText(item.reason))}</span>`).join("")}${failed.map((item) => `<span>失败 ${escapeHtml(minerCacheKindLabel(item.kind))} ${escapeHtml(candidateById.get(item.candidate_id)?.name || "旧矿工文件")}：${escapeHtml(minerCacheReasonText(item.reason))}</span>`).join("")}`;
  } else resultBox.textContent = "";
  $("#minerCacheError").textContent = run.error || "";
  $("#rescanMinerCacheButton").disabled = busy;
  const confirm = $("#confirmMinerCacheCleanupButton");
  confirm.disabled = run.phase !== "ready" || scan?.can_cleanup !== true || !candidates.length || !/^[0-9a-f]{64}$/.test(String(scan?.scan_fingerprint || ""));
  confirm.textContent = run.phase === "cleaning" ? "正在清理…" : "确认清理旧矿工文件";
}

async function waitForMinerCacheCommand(commandId, agentId, action, run, token, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!minerCacheContextCurrent(run, token)) return null;
    const command = await api(`/api/commands/${encodeURIComponent(commandId)}`);
    if (!minerCacheContextCurrent(run, token)) return null;
    if (command.id !== commandId || command.agent_id !== agentId || command.action !== action) {
      throw new Error("任务与当前设备不匹配，已停止展示结果");
    }
    if (["succeeded", "failed", "cancelled", "expired"].includes(command.status)) return command;
    await new Promise((resolve) => window.setTimeout(resolve, 1_000));
  }
  throw new Error("等待 Agent 返回超时；任务可能仍在执行，请稍后查看操作记录");
}

async function scanMinerCache() {
  const run = state.minerCacheCleanup;
  if (!run || !minerCacheContextCurrent(run, state.minerCacheCleanupToken)) return;
  const token = ++state.minerCacheCleanupToken;
  run.phase = "scanning"; run.scan = null; run.scanCommandId = null; run.result = null; run.error = ""; run.message = "";
  renderMinerCacheCleanup();
  try {
    const queued = await api(`/api/agents/${encodeURIComponent(run.agentId)}/commands`, {
      method: "POST", body: { action: MINER_CACHE_SCAN_ACTION, payload: {} },
    });
    if (!minerCacheContextCurrent(run, token)) return;
    if (queued.agent_id !== run.agentId || queued.action !== MINER_CACHE_SCAN_ACTION || !queued.id) throw new Error("扫描任务与当前设备不匹配");
    run.scanCommandId = queued.id;
    const command = await waitForMinerCacheCommand(queued.id, run.agentId, MINER_CACHE_SCAN_ACTION, run, token, 120_000);
    if (!command || !minerCacheContextCurrent(run, token)) return;
    const scan = command.result;
    if (!["succeeded", "failed"].includes(command.status)
      || !scan || scan.schema_version !== 1 || !Array.isArray(scan.candidates)
      || !Array.isArray(scan.protected) || !Array.isArray(scan.unclassified)
      || (scan.can_cleanup === true && !/^[0-9a-f]{64}$/.test(String(scan.scan_fingerprint || "")))
      || (command.status === "failed" && scan.can_cleanup !== false)) {
      throw new Error("设备未能完成安全扫描，请检查 Agent 状态后重试");
    }
    run.scan = scan;
    run.scanCompletedAt = command.finished_at;
    run.phase = "ready";
  } catch (error) {
    if (!minerCacheContextCurrent(run, token)) return;
    run.phase = "error"; run.error = error.message; run.message = "扫描失败，尚未清理任何矿工软件。";
  }
  renderMinerCacheCleanup();
}

async function openMinerCacheCleanup() {
  const agent = state.selectedAgent;
  const role = state.user?.role;
  if (!["admin", "operator"].includes(role)) return;
  const reason = minerCacheUnavailableReason(agent, deviceRuntimePreparationReason(agent));
  if (reason) { toast(reason, "error"); return; }
  state.minerCacheCleanupToken += 1;
  state.minerCacheCleanup = {
    agentId: agent.id, agentName: agent.name, authEpoch: state.authEpoch,
    phase: "scanning", scan: null, scanCommandId: null, result: null, error: "", message: "",
  };
  $("#minerCacheDialog").showModal();
  await scanMinerCache();
}

function closeMinerCacheCleanup() {
  if (state.minerCacheCleanup?.phase === "cleaning") {
    toast("清理命令仍可能在设备上执行；可在操作记录中查看结果");
  }
  state.minerCacheCleanupToken += 1;
  state.minerCacheCleanup = null;
  if ($("#minerCacheDialog")?.open) $("#minerCacheDialog").close();
}

async function cleanupMinerCache(event) {
  event.preventDefault();
  const run = state.minerCacheCleanup;
  if (!run || !minerCacheContextCurrent(run, state.minerCacheCleanupToken) || run.phase !== "ready") return;
  const latest = state.agents.find((item) => item.id === run.agentId) || state.selectedAgent;
  const reason = minerCacheUnavailableReason(latest, deviceRuntimePreparationReason(latest));
  if (reason) { run.error = reason; renderMinerCacheCleanup(); return; }
  const scan = run.scan;
  if (!scan || scan.can_cleanup !== true || !scan.candidates?.length || !run.scanCommandId || !/^[0-9a-f]{64}$/.test(String(scan.scan_fingerprint || ""))) return;
  const fingerprint = scan.scan_fingerprint;
  const token = ++state.minerCacheCleanupToken;
  run.phase = "cleaning"; run.error = ""; run.result = null; run.message = "";
  renderMinerCacheCleanup();
  try {
    const queued = await api(`/api/agents/${encodeURIComponent(run.agentId)}/commands`, {
      method: "POST", body: { action: MINER_CACHE_CLEANUP_ACTION, payload: { scan_fingerprint: fingerprint } },
    });
    if (!minerCacheContextCurrent(run, token)) return;
    if (queued.agent_id !== run.agentId || queued.action !== MINER_CACHE_CLEANUP_ACTION || !queued.id || run.scanCommandId === queued.id || run.scan?.scan_fingerprint !== fingerprint) {
      throw new Error("清理任务与本次设备扫描不匹配");
    }
    const command = await waitForMinerCacheCommand(queued.id, run.agentId, MINER_CACHE_CLEANUP_ACTION, run, token, 300_000);
    if (!command || !minerCacheContextCurrent(run, token)) return;
    const result = command.result && typeof command.result === "object" ? command.result : null;
    if (result?.requested_scan_fingerprint && result.requested_scan_fingerprint !== fingerprint) throw new Error("设备返回的扫描指纹不匹配");
    run.result = result?.schema_version === 1 ? result : null;
    run.phase = command.status === "succeeded" && result?.state === "succeeded" ? "succeeded"
      : result?.state === "stale_scan" ? "stale"
      : result?.state === "partial" ? "partial" : "error";
    run.message = run.phase === "stale" ? "设备内容已变化，请重新扫描后再确认。"
      : run.phase === "partial" ? "部分旧包未能安全清理，已保留失败项。"
      : run.phase === "error" ? "设备未能完成清理；请查看失败项后重扫。" : "当前使用中的矿工软件与飞行表保持不变。";
    renderMinerCacheCleanup();
    try {
      const updated = await api(`/api/agents/${encodeURIComponent(run.agentId)}`);
      if (minerCacheContextCurrent(run, token) && updated.id === run.agentId) {
        state.agents = state.agents.map((item) => item.id === run.agentId ? updated : item);
        state.selectedAgent = updated;
        refreshSelectedAgentDrawer(false);
        renderDevices();
      }
    } catch { /* The measured result stays visible even if heartbeat refresh is late. */ }
  } catch (error) {
    if (!minerCacheContextCurrent(run, token)) return;
    run.phase = "error"; run.error = error.message;
    run.message = "没有确认清理完成；请重新扫描或查看操作记录。";
    renderMinerCacheCleanup();
  }
}

function agentSupportsMiningResidueRepair(agent) {
  const capabilities = Array.isArray(agent?.capabilities) ? agent.capabilities : [];
  return capabilities.includes(MINING_RESIDUE_REPAIR_CAPABILITY)
    && capabilities.includes(MINING_RESIDUE_SELECTIVE_CAPABILITY);
}

function agentSupportsExplicitResidueCandidate(agent) {
  const capabilities = Array.isArray(agent?.capabilities) ? agent.capabilities : [];
  return capabilities.includes(MINING_RESIDUE_EXPLICIT_CANDIDATE_CAPABILITY);
}

function miningResidueRoleLabel(role) {
  return {
    legacy: "单进程 / 旧运行位",
    primary: "第一矿工进程",
    secondary: "第二矿工进程",
  }[String(role || "")] || String(role || "未知运行位");
}

function miningResidueStatusLabel(status) {
  return {
    no_record: "没有旧运行记录",
    verified_process: "已确认当前矿工",
    exact_replacement: "已确认替代矿工",
    orphan_record: "仅剩旧运行记录",
    preboot_record: "上次开机的旧记录",
    ambiguous: "身份不明确，禁止自动清理",
  }[String(status || "")] || String(status || "状态未知");
}

function miningResidueReasonMessage(reason, hasManualCandidate = false) {
  const text = String(reason || "");
  const exact = {
    "no managed miner process record": "该运行位没有 Agent 管理的矿工记录。",
    "recorded PID identity is verified": "PID、程序路径、创建时间和完整启动参数均已确认。",
    "managed miner record predates the current host boot": "记录来自本次开机之前，不会按旧 PID 结束任何进程。",
    "managed miner identity is ambiguous": "无法唯一证明实际矿工身份，因此不会自动结束进程。",
    "same-name process is not an exact replacement": hasManualCandidate
      ? "发现唯一同名矿工，但其完整启动参数与旧记录不一致；只有你勾选确认后才会进行清理。"
      : "发现同名矿工，但本次扫描未提供可复核的唯一候选，不能人工清理；请重新扫描或更新 Agent。",
  };
  if (exact[text]) return exact[text];
  if (text.includes("exact replacement")) return "找到了唯一且启动参数完全一致的替代矿工，可以安全接管或停止。";
  if (text.includes("complete argv fingerprint")) return "实际进程的完整启动参数与旧记录不一致，禁止自动清理。";
  if (text.includes("no process matched")) return "未发现与旧记录完全匹配的矿工进程，可以只删除旧记录。";
  if (text.includes("more than one") || text.includes("multiple")) return "发现多个可能的矿工进程，无法唯一确认，禁止自动清理。";
  return text || "Agent 未提供更多说明。";
}

function miningResidueExplicitCandidateIsSelectable(finding, candidate) {
  const role = String(finding?.role || "");
  const targetFingerprint = String(finding?.target_fingerprint || "");
  const candidateFingerprint = String(candidate?.candidate_fingerprint || "");
  const candidatePid = Number(candidate?.pid);
  return finding?.status === "ambiguous"
    && ["legacy", "primary", "secondary"].includes(role)
    && typeof finding?.miner_type === "string"
    && finding.miner_type.length > 0
    && /^[0-9a-f]{64}$/.test(targetFingerprint)
    && /^[0-9a-f]{64}$/.test(candidateFingerprint)
    && Number.isSafeInteger(candidatePid)
    && candidatePid > 0;
}

function miningResidueFindingMarkup(finding, allowExplicitCandidate) {
  const clearable = finding?.clearable === true;
  const status = String(finding?.status || "unknown");
  const role = String(finding?.role || "");
  const manualCandidates = Array.isArray(finding?.manual_candidates) ? finding.manual_candidates : [];
  // A schema-v3 candidate fingerprint is authoritative for this scan. Do not
  // leave an otherwise valid candidate disabled merely because the heartbeat
  // capability list is stale after an Agent update.
  const manualCandidate = manualCandidates.length === 1
    && miningResidueExplicitCandidateIsSelectable(finding, manualCandidates[0])
    ? manualCandidates[0]
    : null;
  const automaticallySelectable = clearable && ["legacy", "primary", "secondary"].includes(role);
  const selectable = automaticallySelectable || manualCandidate !== null;
  const processText = manualCandidate
    ? `候选 PID ${Number(manualCandidate.pid)}`
    : finding?.verified_pid != null
    ? `确认 PID ${Number(finding.verified_pid)}`
    : finding?.record_pid != null
      ? `记录 PID ${Number(finding.record_pid)}`
      : "无 PID";
  const minerText = [finding?.miner_type, finding?.flight_sheet_id].filter(Boolean).map(String).join(" · ");
  const statusText = manualCandidate ? "已识别矿工，等待人工确认" : miningResidueStatusLabel(status);
  const capabilityNote = manualCandidate && !allowExplicitCandidate
    ? " 当前能力列表尚未同步，但本次扫描已返回可复核的候选指纹。"
    : "";
  const riskText = manualCandidate
    ? `这是风险清理：勾选后，Agent 会再次核验 PID、创建时间、真实路径及受信任矿工目录，再结束该进程。${capabilityNote}`
    : miningResidueReasonMessage(finding?.reason, false);
  const selectionState = automaticallySelectable ? " checked" : selectable ? "" : " disabled";
  const candidateFingerprintAttribute = manualCandidate
    ? ` data-candidate-fingerprint="${escapeHtml(manualCandidate.candidate_fingerprint)}"`
    : "";
  const stateClass = manualCandidate ? "is-manual-candidate" : selectable ? "is-selectable" : status === "ambiguous" ? "is-ambiguous" : "";
  return `<label class="mining-residue-finding ${stateClass}">
    <input type="checkbox" data-mining-residue-target data-role="${escapeHtml(role)}" data-target-fingerprint="${escapeHtml(finding?.target_fingerprint || "")}"${candidateFingerprintAttribute}${selectionState}>
    <span><strong>${escapeHtml(miningResidueRoleLabel(role))}</strong><b>${escapeHtml(statusText)}</b><small>${escapeHtml([processText, minerText].filter(Boolean).join(" · "))}</small><em>${escapeHtml(riskText)}</em></span>
  </label>`;
}

function renderMiningResidueRepair() {
  const repair = state.miningResidueRepair;
  const findings = Array.isArray(repair?.scan?.findings) ? repair.scan.findings : [];
  const busy = ["scanning", "cleaning", "verifying", "restarting"].includes(repair?.phase);
  const phaseCopy = {
    scanning: ["正在安全扫描", "只读取 Agent 管理的固定运行记录和进程身份，不会结束进程。"],
    ready: ["扫描完成", "请选择已自动确认的运行位；如显示唯一同名候选，也可在风险提示下明确选择。"],
    cleaning: ["正在清理已确认的矿工与旧记录", "Agent 会在执行前再次核对扫描指纹；状态变化时会自动中止。"],
    verifying: ["正在复查矿工残留", "只有复查确认三个固定运行位均无旧记录，才会重新启动挖矿。"],
    restarting: ["清理完成，正在重新启动挖矿", "正在重新应用设备上保存的飞行表和控制端保存的 NVML OC。"],
    succeeded: ["修复完成", repair?.message || "矿工状态已经重建。"],
    incomplete: ["只完成了安全部分", repair?.message || "仍有身份不明确的运行位，未自动重启挖矿。"],
    error: ["操作未完成", repair?.message || "请查看错误后重新扫描。"],
  }[repair?.phase] || ["准备扫描", "等待开始。"];

  const summary = $("#miningResidueSummary");
  if (summary) {
    summary.className = `mining-residue-summary ${repair?.phase === "error" || repair?.phase === "incomplete" ? "warning" : ""}`;
    summary.innerHTML = `${busy ? '<span class="spinner-dot"></span>' : ""}<div><strong>${escapeHtml(phaseCopy[0])}</strong><small>${escapeHtml(phaseCopy[1])}</small></div>`;
  }
  const list = $("#miningResidueFindings");
  if (list) {
    list.innerHTML = findings.length
      ? findings.map((finding) => miningResidueFindingMarkup(finding, repair?.explicitCandidate === true)).join("")
      : `<div class="mining-residue-empty">${repair?.phase === "scanning" ? "等待 Agent 返回扫描结果…" : "尚无扫描结果"}</div>`;
  }
  const confirmHint = $("#miningResidueConfirmHint");
  if (confirmHint) confirmHint.textContent = repair?.agentName ? `请输入“${repair.agentName}”以确认。` : "输入设备名称以确认。";
  const error = $("#miningResidueError");
  if (error) error.textContent = repair?.error || "";
  const rescan = $("#rescanMiningResidueButton");
  if (rescan) rescan.disabled = busy || !repair?.agentId;
  const cleanup = $("#cleanupMiningResidueButton");
  if (cleanup) {
    cleanup.disabled = busy || !repair?.scan || repair?.phase === "succeeded";
    cleanup.textContent = repair?.phase === "cleaning" ? "正在清理…" : repair?.phase === "verifying" ? "正在复查…" : repair?.phase === "restarting" ? "正在重启…" : "确认修复";
  }
  for (const selector of ["#miningResidueConfirmName", "#miningResidueResetSrb", "#miningResidueResetNvml", "#miningResidueRestart"]) {
    const element = $(selector);
    if (element) element.disabled = busy || repair?.phase === "succeeded";
  }
  const close = $("#closeMiningResidueDialogButton");
  if (close) close.disabled = ["cleaning", "verifying", "restarting"].includes(repair?.phase);
}

async function waitForMiningResidueCommand(commandId, token, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  const authEpoch = state.authEpoch;
  const agentId = state.miningResidueRepair?.agentId;
  const stillCurrent = () => token === state.miningResidueRepairToken
    && authEpoch === state.authEpoch && state.miningResidueRepair?.agentId === agentId;
  while (Date.now() < deadline) {
    if (!stillCurrent()) throw new Error("修复流程已取消");
    const command = await api(`/api/commands/${encodeURIComponent(commandId)}`);
    if (!stillCurrent()) throw new Error("修复流程已取消");
    if (["succeeded", "failed", "cancelled", "expired"].includes(command.status)) return command;
    await new Promise((resolve) => window.setTimeout(resolve, 1_000));
  }
  throw new Error("等待 Agent 返回超时；任务仍可能在设备上执行，请稍后刷新任务记录");
}

async function scanMiningResidue() {
  const repair = state.miningResidueRepair;
  if (!repair?.agentId) return;
  const token = ++state.miningResidueRepairToken;
  state.miningResidueRepair = { ...repair, phase: "scanning", scan: null, error: "", message: "" };
  renderMiningResidueRepair();
  let queued = null;
  try {
    queued = await api(`/api/agents/${encodeURIComponent(repair.agentId)}/commands`, {
      method: "POST",
      body: { action: "scan_mining_residue.v1", payload: {} },
    });
    if (token !== state.miningResidueRepairToken) return;
    state.miningResidueRepair.commandId = queued.id;
    const command = await waitForMiningResidueCommand(queued.id, token, 120_000);
    if (command.status !== "succeeded") throw new Error(commandErrorMessage(command));
    const scan = command.result;
    if (!scan || !/^[0-9a-f]{64}$/.test(String(scan.scan_fingerprint || "")) || !Array.isArray(scan.findings)) {
      throw new Error("Agent 返回的矿工残留扫描结果无效");
    }
    state.miningResidueRepair = { ...state.miningResidueRepair, phase: "ready", scan, commandId: null, error: "" };
  } catch (error) {
    if (token !== state.miningResidueRepairToken) return;
    state.miningResidueRepair = { ...state.miningResidueRepair, phase: "error", commandId: null, error: error.message, message: "扫描失败，未对矿工进程或 OC 做任何更改。" };
  }
  renderMiningResidueRepair();
}

async function openMiningResidueRepair() {
  const agent = state.selectedAgent;
  if (!agent || agent.status !== "online" || !agentSupportsMiningResidueRepair(agent)) {
    toast("当前设备不支持安全的挖矿状态修复，请先更新 Agent 并保持在线", "error");
    return;
  }
  state.miningResidueRepair = {
    agentId: agent.id,
    agentName: agent.name,
    phase: "scanning",
    scan: null,
    error: "",
    message: "",
    explicitCandidate: agentSupportsExplicitResidueCandidate(agent),
  };
  $("#miningResidueConfirmName").value = "";
  $("#miningResidueResetSrb").checked = true;
  $("#miningResidueResetNvml").checked = true;
  $("#miningResidueRestart").checked = true;
  $("#miningResidueDialog").showModal();
  await scanMiningResidue();
}

function closeMiningResidueRepair() {
  const repair = state.miningResidueRepair;
  if (["cleaning", "verifying", "restarting"].includes(repair?.phase)) {
    toast("修复任务正在设备上执行，完成前不能关闭此窗口", "error");
    return;
  }
  $("#miningResidueDialog").close();
}

async function cleanupMiningResidue(event) {
  event.preventDefault();
  const repair = state.miningResidueRepair;
  if (!repair?.agentId || !repair?.scan) return;
  if ($("#miningResidueConfirmName").value !== repair.agentName) {
    state.miningResidueRepair.error = `请输入“${repair.agentName}”以确认。`;
    renderMiningResidueRepair();
    return;
  }
  const targets = $$("[data-mining-residue-target]:checked", $("#miningResidueFindings")).map((input) => ({
    role: input.dataset.role,
    target_fingerprint: input.dataset.targetFingerprint,
    ...(input.dataset.candidateFingerprint
      ? { candidate_fingerprint: input.dataset.candidateFingerprint }
      : {}),
  }));
  const resetSrb = $("#miningResidueResetSrb").checked;
  const resetNvml = $("#miningResidueResetNvml").checked;
  const restart = $("#miningResidueRestart").checked;
  if (!targets.length && !resetSrb && !resetNvml) {
    state.miningResidueRepair.error = "请至少选择一个已确认运行位或明确候选进程，或选择一项 OC 硬件复位。";
    renderMiningResidueRepair();
    return;
  }
  if (restart && !targets.length) {
    state.miningResidueRepair.error = "自动重启前必须至少选择一个已确认运行位或明确候选进程；只有 OC 复位时请取消“清理后重新启动挖矿”，避免启动重复矿工。";
    renderMiningResidueRepair();
    return;
  }

  const token = ++state.miningResidueRepairToken;
  state.miningResidueRepair = { ...repair, phase: "cleaning", error: "", message: "" };
  renderMiningResidueRepair();
  let cleanupCompleted = false;
  try {
    const queued = await api(`/api/agents/${encodeURIComponent(repair.agentId)}/commands`, {
      method: "POST",
      body: {
        action: "cleanup_mining_residue.v1",
        payload: {
          scan_fingerprint: repair.scan.scan_fingerprint,
          targets,
          reset_srbminer_oc: resetSrb,
          reset_nvml_oc: resetNvml,
        },
      },
    });
    const cleanupCommand = await waitForMiningResidueCommand(queued.id, token, 300_000);
    if (cleanupCommand.status !== "succeeded") throw new Error(commandErrorMessage(cleanupCommand));
    cleanupCompleted = true;
    if (restart) {
      state.miningResidueRepair = { ...state.miningResidueRepair, phase: "verifying", error: "" };
      renderMiningResidueRepair();
      const verificationCommand = await api(`/api/agents/${encodeURIComponent(repair.agentId)}/commands`, {
        method: "POST",
        body: { action: "scan_mining_residue.v1", payload: {} },
      });
      const verified = await waitForMiningResidueCommand(verificationCommand.id, token, 120_000);
      if (verified.status !== "succeeded") throw new Error(`残留已清理，但重新扫描失败：${commandErrorMessage(verified)}`);
      const verificationScan = verified.result;
      if (!verificationScan || !/^[0-9a-f]{64}$/.test(String(verificationScan.scan_fingerprint || "")) || !Array.isArray(verificationScan.findings)) {
        throw new Error("残留已清理，但 Agent 返回的复查结果无效");
      }
      state.miningResidueRepair = { ...state.miningResidueRepair, scan: verificationScan };
      const unresolved = verificationScan.findings.some((finding) => finding?.status !== "no_record");
      if (unresolved) {
        state.miningResidueRepair = {
          ...state.miningResidueRepair,
          phase: "incomplete",
          error: "",
          message: "已完成所选清理和 OC 复位，但重新扫描仍发现矿工运行记录或候选进程；为避免重复矿工，本次未自动重启。请查看复查结果后再次处理。",
        };
      } else {
        state.miningResidueRepair = { ...state.miningResidueRepair, phase: "restarting", error: "" };
        renderMiningResidueRepair();
        const restartCommand = await api(`/api/agents/${encodeURIComponent(repair.agentId)}/commands`, {
          method: "POST",
          body: { action: "restart_mining", payload: {} },
        });
        const restarted = await waitForMiningResidueCommand(restartCommand.id, token, 180_000);
        if (restarted.status !== "succeeded") throw new Error(`残留已清理，但重新启动挖矿失败：${commandErrorMessage(restarted)}`);
        state.miningResidueRepair = {
          ...state.miningResidueRepair,
          phase: "succeeded",
          error: "",
          message: "旧运行记录已安全处理，飞行表与控制端保存的 OC 已重新应用。",
        };
      }
    } else {
      state.miningResidueRepair = {
        ...state.miningResidueRepair,
        phase: "succeeded",
        error: "",
        message: "旧运行记录已安全处理；按你的选择，本次没有重新启动挖矿。",
      };
    }
    toast(state.miningResidueRepair.message);
    await refreshAll(true);
  } catch (error) {
    if (token !== state.miningResidueRepairToken) return;
    state.miningResidueRepair = {
      ...state.miningResidueRepair,
      phase: "error",
      error: error.message,
      message: cleanupCompleted
        ? "安全清理已经完成，但后续重新启动未完成；可以重新扫描后再试。"
        : "清理没有完成；身份不明确的进程不会被结束。",
    };
  }
  renderMiningResidueRepair();
}

function minerLogLineKind(line) {
  const text = String(line || "");
  if (/\b(?:error|fatal|failed|failure|exception|panic|invalid|rejected|denied|crash(?:ed)?)\b|connection\s+(?:lost|closed)/i.test(text)) {
    return { name: "error", label: "ERROR" };
  }
  if (/\b(?:warn(?:ing)?|retry(?:ing)?|stale|timeout|unsupported|disabled)\b/i.test(text)) {
    return { name: "warning", label: "WARN" };
  }
  if (/\b(?:share(?:s)?|accepted)\b/i.test(text)) {
    return { name: "share", label: "SHARE" };
  }
  if (/\b(?:hash\s*rate|hashrate|speed)\b|\b\d+(?:\.\d+)?\s*[kmgtep]?h\s*\/\s*s\b/i.test(text)) {
    return { name: "hashrate", label: "RATE" };
  }
  if (/\b(?:pool|stratum|new\s+job|difficulty|diff|connected|connecting)\b/i.test(text)) {
    return { name: "pool", label: "POOL" };
  }
  if (/\b(?:success(?:ful(?:ly)?)?|ready|started|online)\b/i.test(text)) {
    return { name: "success", label: "OK" };
  }
  return { name: "info", label: "INFO" };
}

function liveMinerLogOverlap(previous, current) {
  const maximum = Math.min(previous.length, current.length);
  for (let size = maximum; size > 0; size -= 1) {
    let matches = true;
    for (let index = 0; index < size; index += 1) {
      if (previous[previous.length - size + index] !== current[index]) {
        matches = false;
        break;
      }
    }
    if (matches) return size;
  }
  return 0;
}

function appendLiveMinerLogSnapshot(session, result) {
  const document = result && typeof result === "object" ? result : {};
  const capturedAt = String(document.captured_at || new Date().toISOString());
  const logs = Array.isArray(document.logs) ? document.logs.slice(0, 8) : [];
  let appended = 0;
  logs.forEach((entry) => {
    // Dual mining reports one entry per (role, source); key the dedup state
    // by both so the primary and secondary legs never collide.
    const role = ["primary", "secondary"].includes(entry?.role) ? entry.role : "";
    const source = String(entry?.source || "miner").slice(0, 256);
    const dedupKey = `${role}:${source}`;
    const current = Array.isArray(entry?.lines)
      ? entry.lines.slice(0, 500).map((line) => String(line).slice(0, 4096))
      : [];
    const previous = session.previousBySource.get(dedupKey) || [];
    const overlap = liveMinerLogOverlap(previous, current);
    current.slice(overlap).forEach((line) => {
      const sequence = session.dropped + session.lines.length;
      session.lines.push({ role, source, line, capturedAt, sequence });
      appended += 1;
    });
    session.previousBySource.set(dedupKey, current);
  });
  const overflow = Math.max(0, session.lines.length - 2000);
  if (overflow) {
    session.lines.splice(0, overflow);
    session.dropped += overflow;
  }
  session.lastResult = document;
  session.lastReceivedAt = capturedAt;
  return appended;
}

function liveMinerLogRoleLabel(role) {
  if (role === "primary") return "主挖";
  if (role === "secondary") return "副挖";
  return "矿工";
}

// Wrap numeric tokens (hashrates, percentages, counts) so the monospace
// stream reads like highlighted source. Runs on the escaped string, split
// around HTML entities so their digits are never rewrapped.
function liveMinerLogHighlight(escapedText) {
  return escapedText
    .split(/(&#039;|&quot;|&lt;|&gt;|&amp;)/)
    .map((chunk) => /^(?:&#039;|&quot;|&lt;|&gt;|&amp;)$/.test(chunk)
      ? chunk
      : chunk.replace(
          /(\d[\d.,]*\s*[kmgtp]?h\/s|\d[\d.,]*%|\d[\d.,]*)/gi,
          '<span class="miner-live-num">$1</span>',
        ))
    .join("");
}

function liveMinerLogLineMarkup(entry, options) {
  const opts = options || {};
  const kind = minerLogLineKind(entry.line);
  const captured = new Date(entry.capturedAt);
  const time = Number.isNaN(captured.getTime())
    ? "--:--:--"
    : captured.toLocaleTimeString("zh-CN", { hour12: false });
  const roleBadge = opts.showRole
    ? `<span class="miner-live-role is-${escapeHtml(entry.role || "legacy")}" title="${escapeHtml(entry.role || "miner")} · ${escapeHtml(entry.source)}">${liveMinerLogRoleLabel(entry.role)}</span>`
    : "";
  return `<div class="miner-live-line${opts.showRole ? " has-role" : ""} is-${kind.name}" data-log-sequence="${Number.isSafeInteger(entry.sequence) ? entry.sequence : ""}"><span class="miner-live-kind">${kind.label}</span><span class="miner-live-time">${time}</span>${roleBadge}<span class="miner-live-text">${liveMinerLogHighlight(escapeHtml(entry.line))}</span></div>`;
}

function renderLiveMinerLogs() {
  const session = state.liveMinerLog;
  const output = $("#liveMinerLogOutput");
  if (!session || !output) return;
  const statusLabels = {
    opening: "正在启动",
    queueing: "正在请求日志",
    waiting: "等待 Agent 返回",
    receiving: "实时接收中",
    retrying: "连接中断，正在重试",
  };
  const status = $("#liveMinerLogStatus");
  const detail = $("#liveMinerLogDetail");
  if (status) {
    status.textContent = statusLabels[session.status] || "实时接收中";
    status.className = `miner-live-status ${session.status === "retrying" ? "is-error" : "is-active"}`;
  }
  if (detail) {
    const miner = String(session.lastResult?.miner_type || "等待识别矿工");
    const last = session.lastReceivedAt ? formatTime(session.lastReceivedAt) : "尚未收到快照";
    detail.textContent = `${miner} · ${session.lines.length} 行 · ${last}${session.dropped ? ` · 已丢弃最早 ${session.dropped} 行` : ""}`;
  }
  // Tabs exist only when dual mining streams more than one leg; a single
  // miner keeps the view clean without any tab bar.
  const roles = [...new Set(session.lines.map((entry) => entry.role || "legacy"))];
  const tabs = $("#liveMinerLogTabs");
  if (tabs) {
    if (roles.length > 1) {
      if (!roles.includes(session.activeTab)) session.activeTab = "all";
      const order = ["primary", "secondary", "legacy"];
      const sorted = roles.sort((a, b) => order.indexOf(a) - order.indexOf(b));
      const markup = [
        `<button type="button" class="miner-live-tab${session.activeTab === "all" ? " is-active" : ""}" data-tab="all">全部</button>`,
        ...sorted.map((role) => `<button type="button" class="miner-live-tab${session.activeTab === role ? " is-active" : ""}" data-tab="${escapeHtml(role)}">${liveMinerLogRoleLabel(role)}</button>`),
      ].join("");
      if (tabs.innerHTML !== markup) {
        const focused = tabs.ownerDocument?.activeElement;
        const focusedTab = focused?.dataset?.tab && tabs.contains(focused) ? focused.dataset.tab : null;
        tabs.innerHTML = markup;
        if (focusedTab) {
          const buttons = tabs.querySelectorAll("[data-tab]");
          ([...buttons].find(button => button.dataset.tab === focusedTab) || buttons[0])?.focus({preventScroll: true});
        }
      }
      tabs.hidden = false;
    } else {
      session.activeTab = "all";
      tabs.hidden = true;
      tabs.innerHTML = "";
    }
  }
  const activeTab = session.activeTab === "all" ? null : session.activeTab;
  const visible = activeTab
    ? session.lines.filter((entry) => (entry.role || "legacy") === activeTab)
    : session.lines;
  const prefix = minerLogErrorMarkup(session.lastResult);
  const empty = visible.length ? "" : activeTab ? `${liveMinerLogRoleLabel(activeTab)}暂无日志，等待新行…` : session.message || "等待设备返回矿工日志…";
  const view = {tab: session.activeTab, first: visible[0], last: visible.at(-1), prefix, empty};
  const previous = session.renderedView;
  // Lines only append or expire from the front. Queue/poll status changes
  // can update the header without replacing an unchanged log or its selection.
  if (previous && previous.tab === view.tab && previous.first === view.first && previous.last === view.last
      && previous.prefix === prefix && previous.empty === empty) return;
  // Follow the stream only when the user is already at the bottom, so
  // scrolling back through history is not hijacked by new lines.
  const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 32;
  const keepPosition = !nearBottom && previous && previous.tab === view.tab && view.first
    && (previous.first !== view.first || previous.prefix !== prefix);
  const anchor = keepPosition ? `[data-log-sequence="${view.first.sequence}"]` : null;
  const oldTop = anchor ? output.querySelector(anchor)?.getBoundingClientRect().top : null;
  if (visible.length) {
    output.innerHTML = prefix
      + visible.map((entry) => liveMinerLogLineMarkup(entry, { showRole: !activeTab })).join("");
    if (nearBottom || previous?.tab !== view.tab) output.scrollTop = output.scrollHeight;
    else if (anchor) {
      const newTop = output.querySelector(anchor)?.getBoundingClientRect().top;
      output.scrollTop = Number.isFinite(oldTop) && Number.isFinite(newTop)
        ? output.scrollTop + newTop - oldTop : 0;
    }
  } else {
    output.innerHTML = prefix + `<div class="miner-live-empty">${escapeHtml(empty)}</div>`;
  }
  session.renderedView = view;
}

function logPollingDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      const error = new Error("log polling stopped");
      error.name = "AbortError";
      reject(error);
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);
    const abort = () => {
      window.clearTimeout(timer);
      const error = new Error("log polling stopped");
      error.name = "AbortError";
      reject(error);
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

function liveMinerLogSessionActive(session) {
  return state.liveMinerLog === session
    && session.active
    && session.token === state.liveMinerLogToken
    && $("#liveMinerLogDialog")?.open;
}

async function requestLiveMinerLogSnapshot(session) {
  const agent = state.agents.find((item) => item.id === session.agentId);
  const unavailable = minerLogUnavailableReason(agent);
  if (unavailable) throw new Error(unavailable);
  session.status = "queueing";
  session.message = "正在创建安全日志采集任务…";
  renderLiveMinerLogs();
  const queued = await api(`/api/agents/${encodeURIComponent(session.agentId)}/commands?live=true`, {
    method: "POST",
    body: { action: "collect_miner_logs", payload: {} },
    signal: session.controller.signal,
  });
  if (!liveMinerLogSessionActive(session)) {
    await cancelMinerLogCommand(queued.id);
    return null;
  }
  session.commandId = queued.id;
  session.status = "waiting";
  session.message = "任务已下发，等待下一次 Agent 心跳…";
  renderLiveMinerLogs();
  const deadline = Date.now() + 45_000;
  while (Date.now() < deadline && liveMinerLogSessionActive(session)) {
    await logPollingDelay(1_000, session.controller.signal);
    const command = await api(`/api/commands/${encodeURIComponent(queued.id)}`, {
      signal: session.controller.signal,
    });
    if (command.status === "succeeded") {
      session.commandId = null;
      return command.result;
    }
    if (["failed", "cancelled", "expired"].includes(command.status)) {
      session.commandId = null;
      throw new Error(command.error || `日志任务状态：${commandStatus(command.status)}`);
    }
  }
  if (liveMinerLogSessionActive(session)) {
    session.commandId = null;
    await cancelMinerLogCommand(queued.id);
    throw new Error("等待 45 秒仍未收到日志，设备可能离线");
  }
  return null;
}

async function runLiveMinerLogLoop(session) {
  while (liveMinerLogSessionActive(session)) {
    try {
      const result = await requestLiveMinerLogSnapshot(session);
      if (!result || !liveMinerLogSessionActive(session)) return;
      const appended = appendLiveMinerLogSnapshot(session, result);
      session.status = "receiving";
      session.message = appended ? "" : "已连接；当前没有新的日志行。";
      renderLiveMinerLogs();
      await logPollingDelay(750, session.controller.signal);
    } catch (error) {
      if (error?.name === "AbortError" || !liveMinerLogSessionActive(session)) return;
      session.commandId = null;
      session.status = "retrying";
      session.message = error.message;
      renderLiveMinerLogs();
      try {
        await logPollingDelay(3_000, session.controller.signal);
      } catch (delayError) {
        if (delayError?.name === "AbortError") return;
        throw delayError;
      }
    }
  }
}

function stopLiveMinerLogs() {
  const session = state.liveMinerLog;
  state.liveMinerLogToken += 1;
  if (!session) return;
  session.active = false;
  state.liveMinerLog = null;
  session.controller.abort();
  const commandId = session.commandId;
  session.commandId = null;
  if (commandId && state.token) void cancelMinerLogCommand(commandId);
}

function openLiveMinerLogs() {
  const agent = state.selectedAgent;
  const unavailable = minerLogUnavailableReason(agent);
  if (unavailable) { toast(unavailable, "error"); return; }
  stopLiveMinerLogs();
  const dialog = $("#liveMinerLogDialog");
  const token = ++state.liveMinerLogToken;
  const session = {
    token,
    agentId: agent.id,
    agentName: agent.name,
    active: true,
    status: "opening",
    message: "等待设备返回矿工日志…",
    commandId: null,
    controller: new AbortController(),
    previousBySource: new Map(),
    lines: [],
    dropped: 0,
    lastResult: null,
    lastReceivedAt: null,
    activeTab: "all",
  };
  state.liveMinerLog = session;
  $("#liveMinerLogAgentName").textContent = agent.name;
  if (!dialog.open) dialog.showModal();
  renderLiveMinerLogs();
  void runLiveMinerLogLoop(session);
}

function clearLiveMinerLogs() {
  const session = state.liveMinerLog;
  if (!session) return;
  session.lines = [];
  session.dropped = 0;
  session.message = "已清空显示，等待新的日志行…";
  renderLiveMinerLogs();
}

function agentCpuModel(agent) {
  const inventory = agent?.inventory && typeof agent.inventory === "object" ? agent.inventory : {};
  const metrics = agent?.metrics && typeof agent.metrics === "object" ? agent.metrics : {};
  const cpu = metrics.cpu && typeof metrics.cpu === "object" ? metrics.cpu : {};
  const model = [inventory.cpu_model, inventory.processor, cpu.model, cpu.model_name]
    .find((value) => typeof value === "string" && value.trim());
  return model ? model.trim() : "型号未上报";
}

function agentMemoryTotal(agent) {
  const metrics = agent?.metrics && typeof agent.metrics === "object" ? agent.metrics : {};
  const memory = metrics.memory && typeof metrics.memory === "object" ? metrics.memory : {};
  const inventory = agent?.inventory && typeof agent.inventory === "object" ? agent.inventory : {};
  const total = memory.total_bytes ?? metrics.memory_total_bytes ?? inventory.memory_total_bytes;
  return total == null ? "容量未上报" : formatBytes(total);
}

function agentDiskSnapshot(agent) {
  const disks = Array.isArray(agent?.metrics?.disks) ? agent.metrics.disks.filter((item) => item && typeof item === "object") : [];
  const root = disks.find((item) => item.mountpoint === "/")
    || disks.find((item) => String(item.mountpoint || "").toUpperCase() === "C:\\")
    || disks[0] || null;
  const reportedFree = root?.free_bytes;
  const freeBytes = typeof reportedFree === "number" && Number.isFinite(reportedFree) && reportedFree >= 0
    ? reportedFree : null;
  return {
    freeBytes,
    mountpoint: String(root?.mountpoint || "").slice(0, 64),
    source: freeBytes === null ? "磁盘空间未上报" : agent?.status === "online" ? "最近心跳上报" : "离线上次上报",
  };
}

function minerCacheUnavailableReason(agent, preparationReason = "") {
  if (!agent || agent.status !== "online") return "设备离线，联机后才能扫描旧矿工";
  if (!Array.isArray(agent.capabilities) || !agent.capabilities.includes(MINER_CACHE_CLEANUP_CAPABILITY)) {
    return "此 Agent 不支持安全清理旧矿工，请先更新被控端";
  }
  return preparationReason || "";
}

function deviceDiskMarkup(agent) {
  const disk = agentDiskSnapshot(agent);
  return `<div class="metric-box device-disk-card"><span>磁盘可用空间</span><strong>${disk.freeBytes === null ? "—" : escapeHtml(formatBytes(disk.freeBytes))}</strong><small>${escapeHtml(disk.source)}${disk.mountpoint ? ` · ${escapeHtml(disk.mountpoint)}` : ""}</small></div>`;
}

async function forgetMissingDeviceGpuOc(gpuUuid, button = null) {
  const agent = state.selectedAgent;
  const canOperate = state.user?.role === "operator" || state.user?.role === "admin";
  const entry = gpuMissingDesiredOcEntries(agent).find((candidate) => (
    gpuMetricUuid({ uuid: candidate?.gpu_uuid }) === gpuUuid
  ));
  const index = Number(entry?.setting?.index);
  if (!agent || !entry || !Number.isInteger(index) || index < 0 || index > 31) {
    toast("该缺席 OC 记录已变化，请刷新设备后重试", "error");
    return;
  }
  if (!canOperate || agent.status !== "online" || !agentVersionAtLeast(agent.agent_version, [0, 6, 16])) {
    toast(canOperate ? "设备必须在线且 Agent 不低于 0.6.16" : "当前账号没有删除权限", "error");
    return;
  }
  const name = String(entry.name || `GPU ${index}`);
  if (!window.confirm(`确认处理设备“${agent.name}”的已缺席 OC 记录“${name}”？\n\nGPU UUID: ${gpuUuid}\n\n该 UUID 当前未被设备上报。本操作只删除控制端保存记录，不会向任何显卡下发恢复默认命令。`)) return;
  if (!window.confirm(`再次确认永久删除 GPU UUID ${gpuUuid} 的已缺席 OC 记录？\n\n只有确认该显卡已经永久移除或更换后才能继续。`)) return;
  if (button) button.disabled = true;
  try {
    const result = await api(`/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/${index}?gpu_uuid=${encodeURIComponent(gpuUuid)}&forget_stale=true`, { method: "DELETE" });
    if (result?.status !== "deleted" || result?.reset_command !== null) {
      throw new Error("控制端未确认缺席 OC 记录已在不触碰硬件的情况下删除");
    }
    toast(`已删除 ${name} 的缺席 OC 记录；未向设备下发硬件任务`);
    await refreshAll(true);
  } catch (requestError) {
    toast(requestError.message, "error");
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

async function deleteDeviceGpuOc() {
  const target = state.deviceGpuOcTarget;
  if (target?.pending) return;
  const agent = target ? state.agents.find((item) => item.id === target.agentId) : null;
  if (deviceRuntimePreparationReason(agent)) { $("#deviceGpuOcError").textContent = deviceRuntimePreparationReason(agent); return; }
  const error = $("#deviceGpuOcError");
  error.textContent = "";
  if (!target || !agent || !target.hasSaved) {
    error.textContent = "控制端没有这张卡的已保存 OC 参数。";
    return;
  }
  const currentGpu = (Array.isArray(agent.metrics?.gpus) ? agent.metrics.gpus : [])
    .find((gpu) => gpuMetricUuid(gpu) === target.uuid);
  if (currentGpu?.driver_available === false) {
    error.textContent = "显卡驱动异常，当前不能下发 OC 恢复默认任务";
    return;
  }
  if (!window.confirm(`确认删除 ${target.name}（GPU ${target.index}）在控制端保存的 NVML OC 参数，并将该卡恢复默认值？`)) return;
  setDeviceGpuOcPending(target, true);
  try {
    const result = await api(`/api/agents/${encodeURIComponent(agent.id)}/gpu-oc/${target.index}?gpu_uuid=${encodeURIComponent(target.uuid)}`, { method: "DELETE" });
    if (state.deviceGpuOcTarget !== target) return;
    if (result?.status !== "reset_pending" || !result?.reset_command) {
      throw new Error("控制端未确认 GPU 恢复默认任务已排队");
    }
    $("#deviceGpuOcDialog").close();
    state.deviceGpuOcTarget = null;
    toast(`GPU ${target.index} 恢复默认任务已排队，成功后才删除保存记录`);
    await refreshAll(true);
  } catch (requestError) {
    if (state.deviceGpuOcTarget === target) error.textContent = requestError.message;
  } finally {
    setDeviceGpuOcPending(target, false);
  }
}

function miningAutostartMarkup(agent, canOperate) {
  const capable = agentVersionAtLeast(agent?.agent_version, [0, 5, 2]);
  const enabled = agent?.mining_autostart_enabled === true;
  const statusLabel = enabled ? "已开启" : "未开启";
  const statusClass = enabled ? "enabled" : "disabled";
  const unsupportedReason = "需要 RigPilot Agent 0.5.2 或更高版本";
  if (!canOperate) {
    return `<div class="mining-autostart-card"><span class="status-pill ${statusClass}">${statusLabel}</span><p>${capable ? "控制端检测到设备进入新的系统启动周期后，只下发一次重启挖矿。" : unsupportedReason}</p></div>`;
  }
  const targetEnabled = !enabled;
  const label = enabled ? "关闭开机自启挖矿" : "开启开机自启挖矿";
  const detail = capable
    ? (enabled ? "仅关闭下次开机自动启动；不会停止当前矿工" : "保存到控制端；设备离线也可预先设置")
    : unsupportedReason;
  return `<button class="action-button mining-autostart-action${enabled ? " is-enabled" : ""}" type="button" data-mining-autostart-enabled="${targetEnabled}"${capable ? "" : ` disabled title="${escapeHtml(unsupportedReason)}"`}><strong>${label}</strong><span><b class="status-pill ${statusClass}">${statusLabel}</b>${escapeHtml(detail)}</span></button>`;
}

async function configureMiningAutostart(event) {
  const agent = state.selectedAgent;
  const button = event.currentTarget;
  const enabled = button?.dataset.miningAutostartEnabled === "true";
  if (!agent || !agentVersionAtLeast(agent.agent_version, [0, 5, 2])) {
    toast("当前 Agent 不支持开机自启挖矿，请先更新被控端", "error");
    return;
  }
  const authEpoch = state.authEpoch;
  const stillCurrent = () => authEpoch === state.authEpoch && state.selectedAgent?.id === agent.id;
  button.disabled = true;
  try {
    const response = await api(`/api/agents/${encodeURIComponent(agent.id)}/mining-autostart`, {
      method: "PATCH",
      body: { enabled },
    });
    if (!stillCurrent()) return;
    const updated = response?.agent && typeof response.agent === "object"
      ? response.agent
      : (response?.id === agent.id ? response : null);
    if (updated) {
      state.agents = state.agents.map((item) => item.id === agent.id ? updated : item);
      state.selectedAgent = updated;
      renderDrawer(updated);
    }
    toast(enabled ? "已开启开机自启挖矿" : "已关闭开机自启挖矿");
    await refreshAll(true);
  } catch (error) {
    if (!stillCurrent()) return;
    toast(error.message, "error");
    if (button?.isConnected) button.disabled = false;
  }
}

function renderDeviceHashrateHistory() {
  const container = $("#deviceHashrateChart");
  const agent = state.selectedAgent;
  if (!container || !agent) return;
  const view = state.deviceHashrateHistory?.agentId === agent.id ? state.deviceHashrateHistory : null;
  if (!view || view.loading && !view.data) {
    container.innerHTML = '<div class="device-hashrate-empty">正在读取此设备的 24 小时历史…</div>';
    return;
  }
  if (view.error) {
    container.innerHTML = `<div class="device-hashrate-empty">${escapeHtml(view.error)}</div>`;
    return;
  }
  const data = view.data && view.data.agent_id === agent.id ? view.data : null;
  const series = (Array.isArray(data?.series) ? data.series : [])
    .map((item) => ({ item, points: hashrateHistoryPoints(item) }))
    .filter((entry) => entry.points.length);
  if (!series.length) {
    container.innerHTML = '<div class="device-hashrate-empty">此设备过去 24 小时暂无可核验算力；不会显示全场曲线。</div>';
    return;
  }
  if (data.series.some((item) => item?.agent_id && item.agent_id !== agent.id)) {
    container.innerHTML = '<div class="device-hashrate-empty">历史序列的设备身份不匹配，请刷新重试。</div>';
    return;
  }
  renderHashrateSeries(container, data);
  container.insertAdjacentHTML("afterbegin", `<div class="device-hashrate-meta"><span>仅 ${escapeHtml(agent.name)} · 按币种显示</span>${view.loading ? '<span>正在更新…</span>' : ""}</div>`);
}

async function loadDeviceHashrateHistory(agentId) {
  if (!agentId || state.selectedAgent?.id !== agentId || state.deviceDetailTab !== "gpus") return;
  const prior = state.deviceHashrateHistory?.agentId === agentId ? state.deviceHashrateHistory : null;
  if (prior && (prior.loading || Date.now() - (prior.fetchedAt || 0) < 60000)) {
    renderDeviceHashrateHistory();
    return;
  }
  const requestId = ++state.deviceHashrateRequestId;
  const authEpoch = state.authEpoch;
  state.deviceHashrateHistory = { agentId, loading: true, data: prior?.data || null, error: "", fetchedAt: prior?.fetchedAt || 0 };
  renderDeviceHashrateHistory();
  try {
    const data = await api(`/api/hashrate-history?hours=24&bucket_minutes=5&agent_id=${encodeURIComponent(agentId)}`);
    if (requestId !== state.deviceHashrateRequestId || authEpoch !== state.authEpoch || state.selectedAgent?.id !== agentId) return;
    if (data?.agent_id !== agentId) throw new Error("历史响应未确认此设备身份，已拒绝显示全场曲线。");
    state.deviceHashrateHistory = { agentId, loading: false, data, error: "", fetchedAt: Date.now() };
  } catch (error) {
    if (requestId !== state.deviceHashrateRequestId || authEpoch !== state.authEpoch || state.selectedAgent?.id !== agentId) return;
    state.deviceHashrateHistory = { agentId, loading: false, data: null, error: error.message || "设备算力历史暂不可用", fetchedAt: Date.now() };
  }
  renderDeviceHashrateHistory();
}

function deviceGpuDriversMarkup(gpus) {
  const groups = new Map();
  gpus.forEach((gpu) => {
    const version = gpuTextValue(gpu?.driver_version, "");
    if (!version) return;
    const vendor = gpuReportedVendor(gpu);
    const vendorLabel = vendor === "nvidia" ? "NVIDIA" : vendor === "amd" ? "AMD" : "厂商未确认";
    const key = `${vendor}:${version}`;
    if (!groups.has(key)) groups.set(key, { label: `${vendorLabel} ${version}`, identities: [] });
    const index = gpuMetricIndex(gpu);
    groups.get(key).identities.push(gpuReportedVendor(gpu) === "amd" || index === null ? gpuDisplayIdentity(gpu) : `GPU ${index}`);
  });
  if (!groups.size) return '<div class="device-gpu-common-driver"><span>驱动未上报</span></div>';
  const showSources = groups.size > 1 || [...groups.values()][0].identities.length !== gpus.length;
  return `<div class="device-gpu-common-driver">${[...groups.values()].map((group) => (
    `<span>驱动 ${escapeHtml(group.label)}${showSources ? ` · ${escapeHtml(group.identities.join("、"))}` : ""}</span>`
  )).join("")}</div>`;
}

function deviceDetailGpuRow(agent, gpu, canOperate = ["admin", "operator"].includes(state.user?.role)) {
  const index = gpuMetricIndex(gpu);
  const uuid = gpuMetricUuid(gpu);
  const driverUnavailable = gpu.driver_available === false;
  const status = driverUnavailable ? "驱动异常"
    : !overviewFreshOnlineAgent(agent) ? "状态未确认" : "已上报";
  const statusClass = driverUnavailable ? "failed"
    : status === "已上报" ? "online" : "offline";
  const identity = gpuReportedVendor(gpu) === "amd" || index === null ? gpuDisplayIdentity(gpu) : `GPU ${index}`;
  const vendorLabel = gpuReportedVendor(gpu) === "amd" ? "AMD · 只读监控"
    : gpuReportedVendor(gpu) === "nvidia" ? "NVIDIA" : "厂商未确认";
  const vendorMarkup = gpuMetricIsNvidia(gpu) && !driverUnavailable && /\bnvidia\b/i.test(gpu.name || "")
    ? "" : `<small class="device-gpu-vendor">${escapeHtml(vendorLabel)}</small>`;
  const reported = (field, suffix) => metric(driverUnavailable ? null : gpu[field], suffix);
  const currentPl = driverUnavailable ? null : gpuNumberValue(gpu?.power_limit_w);
  const fact = (name, label, value, note = "") => (
    `<div class="device-gpu-hardware-fact device-gpu-${name}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>`
    + (note ? `<small>${escapeHtml(note)}</small>` : "") + '</div>'
  );
  const identities = [
    ...(uuid ? [`UUID ${uuid}`] : ["UUID 未上报"]),
    `PCI ${gpuTextValue(gpu?.pci_bus_id, "未上报")}`,
    ...(gpuTextValue(gpu?.driver_version, "") ? [] : ["驱动未上报"]),
  ];
  const driverError = driverUnavailable
    ? `<div class="gpu-driver-error-message">${gpuMetricIsNvidia(gpu) ? "已在 PCI 总线上检测到 NVIDIA 显卡，但 NVIDIA 驱动或 nvidia-smi 未能使用该设备。请检查 BIOS 的 Above 4G Decoding、Resizable BAR、CSM/UEFI 设置及内核日志。" : "已检测到此显卡，但设备尚未确认可用驱动；请查看环境检查和诊断结果。"}</div>` : "";
  return `<tr class="device-gpu-row${driverUnavailable ? " driver-error" : ""}">
    <td class="device-gpu-cell-model" data-label="显卡型号"><div class="device-gpu-model-heading"><strong title="${escapeHtml(gpu.name || identity)}">${escapeHtml(gpu.name || identity)}</strong><span class="device-gpu-model-identity">${escapeHtml(identity)}</span></div><small title="${escapeHtml([gpuBoardLabel(gpu), gpuMemoryIdentityParts(gpu).join(" "), gpuMemoryIdentityProvenance(gpu)].filter(Boolean).join(" · "))}">${escapeHtml([gpuBoardLabel(gpu), gpuMemoryIdentityParts(gpu).join(" ")].join(" · "))}</small>${vendorMarkup}</td>
    <td class="device-gpu-cell-hashrate" data-label="算力">${driverUnavailable ? "—" : gpuMiningHashrateMarkup(agent, gpu) || "—"}</td>
    <td class="device-gpu-cell-thermals" data-label="温度 / 风扇">${fact("temperature", "温度", reported("temperature_c", "°C"))}${fact("fan", "风扇", reported("fan_speed_percent"))}</td>
    <td class="device-gpu-cell-utilization" data-label="负载 / 显存">${fact("load", "GPU 负载", reported("utilization_percent"))}${fact("memory", "显存使用", driverUnavailable ? "—" : gpuMemoryLabel(gpu))}</td>
    <td class="device-gpu-cell-clocks" data-label="核心 / 显存频率">${fact("core-clock", "核心频率", reported("graphics_clock_mhz", " MHz"))}${fact("memory-clock", "显存频率", reported("memory_clock_mhz", " MHz"))}</td>
    <td class="device-gpu-cell-power" data-label="功耗 / PL">${fact("live-power", "功耗", reported("power_w", " W"))}${fact("limits", "PL 最小 / 默认 / 最大", gpuPowerLimitsLabel(gpu), currentPl !== null ? `当前 PL ${metric(currentPl, " W")}` : "")}</td>
    <td class="device-gpu-cell-identity" data-label="硬件身份">${fact("bios", "BIOS", gpuTextValue(gpu?.vbios_version, "未上报"))}<div class="device-gpu-identity-band">${identities.map((value) => `<span class="device-gpu-identifier" title="${escapeHtml(value)}" aria-label="${escapeHtml(value)}">${escapeHtml(value)}</span>`).join("")}</div>${driverError}</td>
    <td class="device-gpu-cell-oc" data-label="OC 记录">${gpuActiveOcMarkup(agent, gpu)}</td>
    <td class="device-gpu-cell-status" data-label="状态 / 操作"><div class="device-gpu-status-actions"><span class="status-pill ${statusClass}">${status}</span>${gpuOcButtonMarkup(agent, gpu, canOperate)}</div></td>
  </tr>`;
}

function deviceFlightSummaryMarkup(agent, canOperate, preparationReason) {
  const status = miningStatusForAgent(agent);
  const snapshot = deviceTableMiningSnapshot(agent);
  const sheet = state.flightSheets.find((item) => item.id === status?.flight_sheet_id);
  const name = status?.flight_sheet_name || sheet?.name || (status?.configured ? "当前飞行表未上报名称" : "尚未配置飞行表");
  const coins = [...new Set(snapshot.rows.map((row) => row.coin).filter((coin) => coin && coin !== "—"))];
  const miner = snapshot.rows[0]?.minerIdentity || null;
  const hiveos = miningStatusUsesHiveos(status);
  const environment = hiveos ? "HiveOS 已停止支持"
    : agent.status === "online" ? "设备在线" : "设备离线";
  const environmentReady = !hiveos && agent.status === "online";
  return `<div class="device-flight-summary"><div><span>当前飞行表</span><strong>${escapeHtml(name)}</strong></div><div><span>币种</span><strong>${escapeHtml(coins.join(" / ") || status?.coin || "—")}</strong></div><div><span>挖矿软件</span><strong>${escapeHtml(miner?.name || status?.miner_name || "—")}</strong><small>${escapeHtml(miner?.version || status?.miner_version || "版本未上报")}${hiveos ? ' <span class="hiveos-badge">HiveOS</span>' : ""}</small></div><div class="device-flight-environment"><span>运行环境</span><strong class="${environmentReady ? "is-ready" : "is-pending"}">${escapeHtml(environment)}</strong></div>${canOperate ? `<button class="text-button" type="button" data-agent-config="flight"${preparationReason ? ` disabled title="${escapeHtml(preparationReason)}"` : ""}>${status?.configured ? "更换飞行表" : "应用飞行表"} →</button>` : ""}</div>`;
}

function switchDeviceDetailTab(tab) {
  if (!["overview", "gpus", "logs"].includes(tab)) return;
  state.deviceDetailTab = tab;
  $("#drawerContent").dataset.activeTab = tab;
  $$('[data-device-tab]').forEach((button) => {
    const active = button.dataset.deviceTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $$("#drawerContent > .drawer-section").forEach((section) => {
    const target = section.classList.contains("device-detail-gpus") || section.classList.contains("device-detail-gpu-missing") ? "gpus"
      : section.classList.contains("device-detail-logs") ? "logs" : "overview";
    section.classList.toggle("hidden", target !== tab);
  });
  if (tab === "logs") loadDeviceActivity(state.selectedAgent?.id);
  if (tab === "gpus") loadDeviceHashrateHistory(state.selectedAgent?.id);
  if (tab === "overview") {
    const agentId = state.selectedAgent?.id;
    window.requestAnimationFrame(() => {
      if (state.selectedAgent?.id !== agentId || state.deviceDetailTab !== "overview") return;
      const points = state.selectedAgentTelemetryId === agentId ? state.selectedAgentTelemetry : [];
      if ($("#telemetryChart")?.getClientRects().length) drawTelemetry(points);
    });
  }
}

function renderDrawer(agent, preserveInputs = true) {
  const content = $("#drawerContent");
  const sameDevice = content.dataset.agentId === agent.id;
  const snapshot = sameDevice && preserveInputs ? captureDrawerState() : null;
  rememberDisclosures(content);
  content.dataset.agentId = agent.id;
  const os = osLabel(agent);
  const metrics = agent.metrics || {};
  const gpus = Array.isArray(metrics.gpus) ? metrics.gpus.filter((gpu) => gpu && typeof gpu === "object") : [];
  const canOperate = state.user?.role === "operator" || state.user?.role === "admin";
  const isAdmin = state.user?.role === "admin";
  const alerts = agent.alerts || [];
  const preparationReason = deviceRuntimePreparationReason(agent);
  const removeFlightSupported = agent.status === "online" && !preparationReason && agentVersionAtLeast(agent.agent_version, [0, 6, 1]);
  const removeFlightReason = preparationReason || (agent.status !== "online"
    ? "设备离线时不能去除飞行表"
    : "需要 RigPilot Agent 0.6.1 或更高版本");
  const residueRepairSupported = agent.status === "online" && agentSupportsMiningResidueRepair(agent);
  const residueRepairReason = agent.status !== "online"
    ? "设备离线时不能修复挖矿状态"
    : "需要支持安全选择性残留清理的新版 Agent";
  const cacheCleanupReason = minerCacheUnavailableReason(agent, preparationReason);
  const bootstrapLabel = agentBootstrapLabel(agent);
  const gpuTelemetry = overviewGpuTelemetry(agent);
  const averageGpuTemperature = gpuTelemetry.temperatures.length
    ? `${Math.round(gpuTelemetry.temperatures.reduce((sum, value) => sum + value, 0) / gpuTelemetry.temperatures.length)}°C`
    : "—";
  if (!gpus.length && state.deviceDetailTab === "gpus") state.deviceDetailTab = "overview";
  $("#drawerContent").innerHTML = `
    <div class="device-hero"><div class="device-hero-copy"><div class="device-hero-title"><h3>${escapeHtml(agent.name)}</h3><span class="status-pill ${escapeHtml(agent.status)}">${statusLabel(agent.status)}</span>${configurationRefreshBadge(agent, true)}${agentUpdateStatusMarkup(agent)}</div><p>${escapeHtml(agent.hostname)} · ${escapeHtml(agent.platform || agent.os_family)} · Agent ${escapeHtml(agent.agent_version)}${bootstrapLabel ? ` · ${escapeHtml(bootstrapLabel)}` : ""}</p></div><div class="device-hero-shortcuts"><button class="btn btn-secondary btn-small" type="button" data-device-tab="logs">日志</button><div class="device-hero-mining-controls">${miningControlButtons(agent)}</div></div></div>
    <div class="device-kpi-strip">
      <div><span>当前算力</span>${deviceMiningColumnMarkup(deviceTableMiningSnapshot(agent), "hashrate")}</div>
      <div><span>已上报 GPU 功耗</span><strong>${gpuTelemetry.powerCount ? overviewFormatPower(gpuTelemetry.powerW) : "—"}</strong><small>不含 CPU / 主机${gpuTelemetry.total > gpuTelemetry.powerCount ? ` · ${gpuTelemetry.total - gpuTelemetry.powerCount} 张未报` : ""}</small></div>
      <div><span>显卡数量</span><strong>${gpus.length || "—"}</strong><small>${agent.status === "online" ? "设备当前上报" : "离线上次上报"}</small></div>
      <div><span>平均 GPU 温度</span><strong>${averageGpuTemperature}</strong><small>${gpuTelemetry.temperatures.length ? `${gpuTelemetry.temperatures.length} 张在线卡已上报` : "温度未确认"}</small></div>
    </div>
    ${deviceFlightSummaryMarkup(agent, canOperate, preparationReason)}
    <div class="device-detail-tabs" role="group" aria-label="设备详情分类"><button type="button" data-device-tab="overview">概览</button><button type="button" data-device-tab="gpus">显卡</button><button type="button" data-device-tab="logs">运行日志</button></div>
    ${alerts.length ? `<div class="alert-list">${alerts.map((alert) => `<div class="alert-item">${escapeHtml(alert.message)}</div>`).join("")}</div>` : ""}
    ${gpus.length ? `<section class="drawer-section device-detail-gpus"><section class="device-hashrate-panel"><h4>24 小时设备算力</h4><div id="deviceHashrateChart" aria-live="polite"><div class="device-hashrate-empty">正在读取此设备的历史…</div></div></section><div class="drawer-section-head"><div><h4>GPU 硬件监控</h4><small>按设备上报显示；NVIDIA 可用参数支持逐卡设置，AMD 显示只读监控</small></div>${deviceGpuDriversMarkup(gpus)}${canOperate ? `<button class="btn btn-secondary btn-small" type="button" data-device-gpu-oc-batch${agent.status === "online" && !preparationReason && agentVersionAtLeast(agent.agent_version, [0, 6, 16]) && batchDeviceGpuCandidates(agent).length ? "" : ` disabled title="${escapeHtml(preparationReason || (agent.status !== "online" ? "设备离线时不能下发 OC" : "需要 Agent 0.6.16+ 且至少上报一张带 UUID 的 NVIDIA GPU"))}"`}>批量 OC</button>` : ""}</div><p class="device-gpu-oc-note device-gpu-oc-common-note">保存、声明和最近成功下发记录均不等同于当前生效参数。</p><div class="table-wrap"><table class="device-table device-gpu-table"><thead><tr><th>显卡型号</th><th>算力</th><th>温度 / 风扇</th><th>负载 / 显存</th><th>频率</th><th>功耗 / PL</th><th>硬件身份</th><th>OC 记录</th><th>状态 / 操作</th></tr></thead><tbody>${gpus.map((gpu) => deviceDetailGpuRow(agent, gpu, canOperate)).join("")}</tbody></table></div></section>` : ""}
    <section class="drawer-section device-detail-mining"><div class="drawer-section-head"><div><h4>挖矿状态</h4><small>由 Agent 上报，异常时可安全扫描并修复旧进程记录</small></div>${canOperate ? `<div class="miner-log-actions"><button class="btn btn-secondary btn-small" type="button" data-agent-action="collect_diagnostics"${agent.status === "online" ? "" : ' disabled title="设备离线，无法采集诊断信息"'}>采集诊断</button><button class="btn btn-secondary btn-small" type="button" data-mining-residue-repair${residueRepairSupported ? "" : ` disabled title="${escapeHtml(residueRepairReason)}"`}>修复挖矿状态</button></div>` : ""}</div><div id="miningStatusContent">${miningStatusMarkup(agent)}</div></section>
    ${minerLogSectionMarkup(agent)}
    <section class="drawer-section device-detail-resources"><div class="drawer-section-head"><div><h4>实时资源</h4>${canOperate && cacheCleanupReason ? `<small class="miner-cache-unavailable">${escapeHtml(cacheCleanupReason)}</small>` : ""}</div>${canOperate ? `<button class="btn btn-secondary btn-small" type="button" data-miner-cache-cleanup${cacheCleanupReason ? ` disabled title="${escapeHtml(cacheCleanupReason)}"` : ""}>清理旧矿工</button>` : ""}</div><div class="metric-grid">
      <div class="metric-box resource-identity"><span>CPU</span><strong class="cpu-resource-summary">${metric(metrics.cpu_percent)}<em>· ${escapeHtml(cpuTemperatureLabel(metrics, "温度未上报"))}</em></strong><small title="${escapeHtml(agentCpuModel(agent))}">${escapeHtml(agentCpuModel(agent))}</small></div>
      <div class="metric-box resource-identity"><span>内存</span><strong>${metric(metrics.memory_percent)}</strong><small>${escapeHtml(agentMemoryTotal(agent))}</small></div>
      ${deviceDiskMarkup(agent)}
      <div class="metric-box"><span>GPU 利用率</span><strong>${gpuUtilizationMetric(gpus)}</strong></div>
      <div class="metric-box"><span>运行时间</span><strong>${formatUptime(metrics.uptime_seconds)}</strong></div>
      <div class="metric-box"><span>GPU 数量</span><strong>${gpus.length || "—"}</strong></div>
      <div class="metric-box"><span>最后在线</span><strong>${escapeHtml(formatTime(agent.last_seen))}</strong></div>
    </div></section>
    <section class="drawer-section device-detail-network"><h4>网络地址</h4>${deviceNetworkMarkup(agent, true)}</section>
    <section class="drawer-section device-detail-telemetry"><h4>近 10 分钟 CPU / 内存</h4><div class="chart-box"><canvas id="telemetryChart"></canvas></div></section>
    ${gpuMissingDesiredOcMarkup(agent, canOperate)}
    ${canOperate ? `<section class="drawer-section device-detail-config"><h4>配置下发</h4><div class="action-grid">
      <button class="action-button accent" data-agent-config="flight"${preparationReason ? ` disabled title="${escapeHtml(preparationReason)}"` : ""}><strong>应用飞行表</strong><span>${preparationReason ? escapeHtml(preparationReason) : "钱包、矿池与本地矿工模板"}</span></button>
      <button class="action-button danger" data-agent-action="remove_flight_sheet"${removeFlightSupported ? "" : ` disabled title="${escapeHtml(removeFlightReason)}"`}><strong>去除飞行表</strong><span>${removeFlightSupported ? "停止挖矿并删除设备上的当前飞行表" : escapeHtml(removeFlightReason)}</span></button>
    </div></section>` : ""}
    <section class="drawer-section device-detail-autostart"><div class="drawer-section-head"><div><h4>开机自启挖矿</h4><small>设置会持久保存；关闭不会停止当前矿工</small></div></div>${miningAutostartMarkup(agent, canOperate)}</section>
    ${canOperate ? `<section class="drawer-section device-detail-actions"><h4>主机操作</h4><div class="action-grid">
      <button class="action-button danger" data-agent-action="reboot"${preparationReason ? ` disabled title="${escapeHtml(preparationReason)}"` : ""}><strong>重启主机</strong><span>短时任务 · 需要确认</span></button>
      <button class="action-button danger" data-agent-action="shutdown"${preparationReason ? ` disabled title="${escapeHtml(preparationReason)}"` : ""}><strong>关闭主机</strong><span>短时任务 · 需要确认</span></button>
    </div></section>` : ""}
    ${isAdmin ? `<section class="drawer-section device-detail-identity"><div class="drawer-section-head"><h4>设备名称</h4><small>管理员可修改</small></div><form id="agentDetailsForm" class="agent-details-form"><label>显示名称<input id="agentDisplayName" maxlength="80" required value="${escapeHtml(agent.name)}"></label><button id="saveAgentDetailsButton" class="btn btn-secondary btn-small" type="submit">保存名称</button></form></section>` : ""}
    <section class="drawer-section device-detail-info"><h4>设备信息</h4><div class="metric-box"><span>设备 ID</span><strong style="font-size:10px;word-break:break-all">${escapeHtml(agent.id)}</strong></div></section>`;
  switchDeviceDetailTab(state.deviceDetailTab);
  $$('[data-device-tab]', $("#drawerContent")).forEach((button) => button.addEventListener("click", () => {
    switchDeviceDetailTab(button.dataset.deviceTab);
  }));
  $$('[data-mining-action]', $("#drawerContent")).forEach((button) => button.addEventListener("click", () => {
    controlMining(button.dataset.miningAction, button.dataset.agentId, button.dataset.agentName, button);
  }));
  $$('[data-agent-action]', $("#drawerContent")).forEach((button) => button.addEventListener("click", () => openAction(button.dataset.agentAction)));
  $$('[data-agent-config]', $("#drawerContent")).forEach((button) => button.addEventListener("click", () => openDeploy(button.dataset.agentConfig)));
  $("[data-device-gpu-oc-batch]", $("#drawerContent"))?.addEventListener("click", openDeviceGpuOcBatch);
  $$('[data-forget-missing-gpu-oc]', $("#drawerContent")).forEach((button) => button.addEventListener("click", () => forgetMissingDeviceGpuOc(button.dataset.forgetMissingGpuOc, button)));
  $("[data-mining-autostart-enabled]", $("#drawerContent"))?.addEventListener("click", configureMiningAutostart);
  $("[data-mining-residue-repair]", $("#drawerContent"))?.addEventListener("click", openMiningResidueRepair);
  $("[data-miner-cache-cleanup]", $("#drawerContent"))?.addEventListener("click", openMinerCacheCleanup);
  bindConfigurationRefreshDismiss($("#drawerContent"));
  bindAgentUpdateDismiss($("#drawerContent"));
  $$('[data-refresh-mining]', $("#drawerContent")).forEach((button) => button.addEventListener("click", refreshAgentStatus));
  $("[data-live-miner-logs]", $("#drawerContent"))?.addEventListener("click", openLiveMinerLogs);
  $("#agentDetailsForm")?.addEventListener("submit", saveAgentDetails);
  restoreDisclosures(content);
  restoreDrawerState(snapshot);
}

function formatUptime(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  if (value < 3600) return `${Math.floor(value / 60)} 分钟`;
  if (value < 86400) return `${Math.floor(value / 3600)} 小时`;
  return `${Math.floor(value / 86400)} 天`;
}

function drawTelemetry(points) {
  const canvas = $("#telemetryChart");
  if (!canvas) return;
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, rect.width * ratio);
  canvas.height = Math.max(1, rect.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  const width = rect.width, height = rect.height, pad = 10;
  context.clearRect(0, 0, width, height);
  context.strokeStyle = themeColor("--chart-grid", "#263038");
  context.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach((fraction) => { context.beginPath(); context.moveTo(pad, height * fraction); context.lineTo(width - pad, height * fraction); context.stroke(); });
  if (!points?.length) {
    context.fillStyle = themeColor("--chart-muted", "#65716f"); context.font = "10px Segoe UI"; context.fillText("等待指标数据", pad, height / 2); return;
  }
  const drawLine = (key, color) => {
    context.beginPath(); context.strokeStyle = color; context.lineWidth = 2;
    points.forEach((point, index) => {
      const x = pad + (index / Math.max(1, points.length - 1)) * (width - pad * 2);
      const y = height - pad - (clampPercent(point.metrics?.[key]) / 100) * (height - pad * 2);
      if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
    });
    context.stroke();
  };
  drawLine("cpu_percent", themeColor("--chart-cpu", "#c9f24d"));
  drawLine("memory_percent", themeColor("--chart-memory", "#6da7ff"));
}

function closeDrawer() {
  state.deviceTelemetryRequestId += 1;
  if ($("#minerCacheDialog")?.open) closeMinerCacheCleanup();
  if ($("#liveMinerLogDialog")?.open) $("#liveMinerLogDialog").close();
  if ($("#deviceGpuOcDialog")?.open) $("#deviceGpuOcDialog").close();
  if ($("#deviceGpuOcBatchDialog")?.open) $("#deviceGpuOcBatchDialog").close();
  state.deviceGpuOcTarget = null;
  state.deviceGpuOcBatchTarget = null;
  state.deviceLogRequestId += 1;
  state.deviceLog = null;
  state.deviceHashrateRequestId += 1;
  state.deviceHashrateHistory = null;
  state.deviceDetailTab = "gpus";
  $("#deviceDrawer")?.classList.remove("open");
  $("#deviceDrawer")?.setAttribute("aria-hidden", "true");
  $("#backdrop")?.classList.add("hidden");
  state.selectedAgent = null;
  state.selectedAgentTelemetry = [];
  state.selectedAgentTelemetryId = null;
}

function openAction(action) {
  if (!state.selectedAgent) return;
  const unavailable = ["stop_mining", "collect_miner_logs", "collect_diagnostics"].includes(action) ? "" : "";
  if (unavailable) { toast(unavailable, "error"); return; }
  if (action === "collect_diagnostics") {
    const unavailable = deviceDiagnosticUnavailableReason(state.selectedAgent);
    if (unavailable) { toast(unavailable, "error"); return; }
  }
  state.selectedAction = action;
  const destructive = ["reboot", "shutdown", "remove_flight_sheet"].includes(action);
  $("#actionTitle").textContent = actionNames[action] || action;
  $("#actionDescription").textContent = action === "remove_flight_sheet"
    ? `该操作将停止 ${state.selectedAgent.name} 当前受管矿工，并删除设备上已应用的飞行表；完成后不会自动重新挖矿。`
    : destructive
      ? `该操作将作用于 ${state.selectedAgent.name}，只在设备及时收到任务时执行。`
      : action === "restart_service" ? "服务器仅传递服务别名；Agent 会再次检查本地允许列表。" : "设备将返回类型化的系统诊断摘要，不会执行任意命令。";
  $("#serviceField").classList.toggle("hidden", action !== "restart_service");
  $("#confirmField").classList.toggle("hidden", !destructive);
  $("#serviceName").value = "";
  $("#confirmName").value = "";
  $("#actionError").textContent = "";
  $("#submitActionButton").className = `btn ${destructive ? "btn-danger" : "btn-primary"}`;
  $("#actionDialog").showModal();
}

async function submitAction() {
  const action = state.selectedAction;
  const agent = state.selectedAgent;
  if (!action || !agent) return;

  if (action === "collect_diagnostics") {
    const unavailable = deviceDiagnosticUnavailableReason(agent);
    if (unavailable) { $("#actionError").textContent = unavailable; return; }
  }
  const payload = {};
  if (action === "restart_service") {
    payload.service = $("#serviceName").value.trim();
    if (!payload.service) { $("#actionError").textContent = "请输入服务别名。"; return; }
  }
  if (["reboot", "shutdown", "remove_flight_sheet"].includes(action)) {
    if ($("#confirmName").value !== agent.name) { $("#actionError").textContent = `请输入“${agent.name}”以确认。`; return; }
  }
  const button = $("#submitActionButton");
  button.disabled = true;
  try {
    await api(`/api/agents/${encodeURIComponent(agent.id)}/commands`, { method: "POST", body: { action, payload } });
    $("#actionDialog").close();
    toast(`${actionNames[action]}已进入队列`);
    await refreshAll(true);
  } catch (error) {
    $("#actionError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function openEnroll() {
  clearEnrollmentSecrets();
  $("#enrollStepForm").classList.remove("hidden");
  $("#enrollStepToken").classList.add("hidden");
  $("#enrollMode").value = "new";
  $("#replacementConfirmName").value = "";
  const replacementSelect = $("#replacementAgent");
  const offlineAgents = state.agents
    .filter((agent) => agent.status === "offline")
    .sort((left, right) => String(left.name || "").localeCompare(String(right.name || ""), "zh-CN"));
  replacementSelect.innerHTML = offlineAgents.length
    ? offlineAgents.map((agent) => `<option value="${escapeHtml(agent.id)}">${escapeHtml(agent.name)} · ${escapeHtml(agent.hostname || agent.id)}</option>`).join("")
    : '<option value="">当前没有离线设备</option>';
  updateEnrollmentMode();
  $("#enrollDialog").showModal();
}

function clearEnrollmentSecrets() {
  state.enrollmentRequestId += 1;
  $("#generateTokenButton").disabled = false;
  $("#enrollmentToken").textContent = "";
  $("#agentCommand").textContent = "";
  $("#tokenExpiry").textContent = "";
  $("#replacementConfirmName").value = "";
}

function updateEnrollmentMode() {
  const replacing = $("#enrollMode").value === "replace";
  $("#enrollNewFields").classList.toggle("hidden", replacing);
  $("#enrollReplacementFields").classList.toggle("hidden", !replacing);
  $("#enrollLabel").disabled = replacing;
  $("#enrollTtl").disabled = replacing;
  $("#replacementAgent").disabled = !replacing;
  $("#replacementConfirmName").disabled = !replacing;
  $("#replacementTtl").disabled = !replacing;
  $("#generateTokenButton").textContent = replacing ? "生成继承码" : "生成注册码";
  updateReplacementConfirmationHint();
}

function updateReplacementConfirmationHint() {
  const selected = state.agents.find((agent) => agent.id === $("#replacementAgent").value);
  $("#replacementConfirmHint").textContent = selected
    ? `请输入“${selected.name}”以确认；旧设备必须保持离线。`
    : "请先让旧受控端离线并刷新设备列表。";
}

async function generateEnrollmentToken() {
  const button = $("#generateTokenButton");
  if (button.disabled) return;
  button.disabled = true;
  const requestId = ++state.enrollmentRequestId;
  const authEpoch = state.authEpoch;
  const stillCurrent = () => requestId === state.enrollmentRequestId && authEpoch === state.authEpoch;
  try {
    const replacing = $("#enrollMode").value === "replace";
    let result;
    if (replacing) {
      const agentId = $("#replacementAgent").value;
      const selected = state.agents.find((agent) => agent.id === agentId);
      if (!selected) throw new Error("当前没有可继承的离线设备。");
      const confirmation = $("#replacementConfirmName").value;
      if (confirmation !== selected.name) throw new Error(`请输入“${selected.name}”以确认。`);
      result = await api(`/api/agents/${encodeURIComponent(agentId)}/replacement-token`, {
        method: "POST",
        body: { confirm_name: confirmation, ttl_minutes: Number($("#replacementTtl").value) },
      });
    } else {
      result = await api("/api/enrollment-tokens", {
        method: "POST",
        body: { label: $("#enrollLabel").value.trim() || "new-device", ttl_minutes: Number($("#enrollTtl").value) },
      });
    }
    if (!stillCurrent()) return;
    $("#enrollmentToken").textContent = result.token;
    $("#tokenExpiry").textContent = `过期时间：${formatTime(result.expires_at, false)}`;
    $("#enrollmentTokenTitle").textContent = replacing ? "设备继承码已生成" : "注册码已生成";
    $("#enrollmentTokenHelp").textContent = replacing
      ? "只显示这一次。把下面内容保存为 U 盘根目录 rigpilot-replace.json，然后启动干净镜像。"
      : "只显示这一次。把服务器地址和注册码填入 Agent 配置。";
    $("#agentCommand").textContent = replacing
      ? JSON.stringify({ schema: "rigpilot.replacement.v1", replacement_token: result.token }, null, 2)
      : [
          "1. 复制 agent/config.example.json 为 agent-config.json，并填写 server_url",
          "2. 将上方注册码设置为环境变量 RIGPILOT_ENROLLMENT_TOKEN",
          "3. python -m agent --config agent-config.json",
        ].join("\n");
    $("#enrollStepForm").classList.add("hidden");
    $("#enrollStepToken").classList.remove("hidden");
  } catch (error) {
    if (stillCurrent()) toast(error.message, "error");
  } finally {
    if (stillCurrent()) button.disabled = false;
  }
}

async function copyToken() {
  const token = $("#enrollmentToken").textContent;
  try {
    await navigator.clipboard.writeText(token);
    toast("注册码已复制");
  } catch {
    toast("无法访问剪贴板，请手动复制", "error");
  }
}

// Each opening owns its draft, even when the same record is opened again.
const resourceEditorOpenings = new WeakMap();

function resetResourceEditor(button) {
  resourceEditorOpenings.set(button, {});
  button.disabled = false;
}

function beginResourceSave(button) {
  if (button.disabled) return null;
  button.disabled = true;
  const opening = resourceEditorOpenings.get(button);
  const epoch = state.authEpoch;
  return () => state.authEpoch === epoch && resourceEditorOpenings.get(button) === opening;
}

function openWalletDialog(wallet = null) {
  resetResourceEditor($("#createWalletButton"));
  $("#walletForm").reset();
  $("#walletError").textContent = "";
  state.editingWalletId = wallet?.id || null;
  $("#walletDialogTitle").textContent = wallet ? "编辑钱包" : "添加钱包";
  $("#createWalletButton").textContent = wallet ? "保存修改" : "保存钱包";
  const coinInput = $("#walletCoin");
  const coinHint = $("#walletCoinHint");
  const isReferenced = Boolean(wallet && state.flightSheets.some((sheet) => sheet.wallet_id === wallet.id || sheet.secondary?.wallet_id === wallet.id));
  coinInput.readOnly = isReferenced;
  coinHint.classList.toggle("hidden", !isReferenced);
  coinHint.textContent = isReferenced ? "该钱包已被飞行表引用，币种不可修改；名称、地址和备注仍可编辑。" : "";
  if (wallet) {
    $("#walletName").value = wallet.name || "";
    coinInput.value = wallet.coin || "";
    $("#walletAddress").value = wallet.address || "";
    $("#walletNote").value = wallet.note || "";
  }
  $("#walletDialog").showModal();
}

function configurationSaveMessage(label, reapply) {
  if (!reapply?.triggered) return `${label}已更新`;
  const queued = Number(reapply.queued) || 0;
  const marked = Number(reapply.marked) || 0;
  const pendingValue = Number(reapply.pending);
  const pending = Number.isFinite(pendingValue)
    ? Math.max(0, pendingValue)
    : Math.max(0, marked - queued);
  const skipped = Number(reapply.skipped) || 0;
  const failed = Number(reapply.failed) || 0;
  if (queued > 0 || pending > 0) {
    const parts = [];
    if (queued > 0) parts.push(`已下发 ${queued} 台`);
    if (pending > 0) parts.push(`${pending} 台待设备上线并开始挖矿后下发`);
    if (skipped + failed > 0) parts.push(`${skipped + failed} 台因冲突或校验失败未下发`);
    return `${label}已更新；${parts.join("，")}`;
  }
  if (skipped + failed > 0) {
    return `${label}已更新；${skipped + failed} 台设备因冲突或校验失败未下发`;
  }
  if (marked > 0) {
    return `${label}已更新；${marked} 台相关设备已标记配置更新`;
  }
  return `${label}已更新；当前没有正在使用相关配置的矿工`;
}

async function saveWallet() {
  const button = $("#createWalletButton");
  const address = $("#walletAddress").value.trim();
  $("#walletError").textContent = "";
  if (/\s/.test(address)) {
    $("#walletError").textContent = "公开地址不能包含空格；请勿填写助记词。";
    return;
  }
  const current = beginResourceSave(button);
  if (!current) return;
  try {
    const editingId = state.editingWalletId;
    const result = await api(editingId ? `/api/wallets/${encodeURIComponent(editingId)}` : "/api/wallets", {
      method: editingId ? "PUT" : "POST",
      body: {
        name: $("#walletName").value.trim(),
        coin: $("#walletCoin").value.trim(),
        address,
        note: $("#walletNote").value.trim(),
      },
    });
    if (!current()) return;
    $("#walletDialog").close();
    state.editingWalletId = null;
    toast(editingId ? configurationSaveMessage("钱包", result.reapply) : "公开钱包地址已保存");
    await refreshAll(true);
  } catch (error) {
    if (current()) $("#walletError").textContent = error.message;
  } finally {
    if (current()) button.disabled = false;
  }
}

function openMinerPackageDialog() {
  state.minerPackageOpening = {};
  $("#createMinerPackageButton").disabled = false;
  $("#createMinerPackageButton").textContent = "保存软件包";
  $("#minerPackageForm").reset();
  $("#minerPackageError").textContent = "";
  populateMinerPackageManagedTypeSelect();
  syncMinerPackageSource("upload");
  $("#minerPackageDialog").showModal();
}

function openAgentReleaseDialog() {
  if (state.user?.role !== "admin") return;
  $("#agentReleaseForm").reset();
  $("#agentReleaseError").textContent = "";
  syncAgentReleasePlatform();
  $("#agentReleaseDialog").showModal();
}

function syncAgentReleasePlatform() {
  const platform = $("#agentReleasePlatform").value;
  const isLinux = platform === "linux";
  const file = $("#agentReleaseFile");
  file.value = "";
  file.accept = isLinux
    ? ".tar.gz,application/gzip,application/x-gzip"
    : ".zip,application/zip,application/x-zip-compressed";
  $("#agentReleaseArchiveLabel").value = isLinux
    ? "TAR.GZ（Linux 在线更新包）"
    : "ZIP（Windows 便携版）";
  $("#agentReleaseArchiveHint").textContent = isLinux
    ? "只能上传专用 OnlineUpdate 包；清单和实际载荷必须仅包含可执行 RigPilotAgent，不能混入安装、服务或更新器脚本。"
    : "包内必须包含 RigPilotAgent.exe 与 Windows 在线更新所需文件。";
  $("#agentReleaseFileHint").textContent = isLinux
    ? "选择文件名以 .tar.gz 结尾的 Linux x64 OnlineUpdate 包；控制端不会解压到文件系统。"
    : "选择 Windows x64 便携版 ZIP；控制端会校验结构、大小和安全路径。";
}

async function uploadAgentRelease() {
  if (state.user?.role !== "admin") return;
  const button = $("#uploadAgentReleaseButton");
  const errorElement = $("#agentReleaseError");
  const version = $("#agentReleaseVersion").value.trim();
  const platform = $("#agentReleasePlatform").value;
  const file = $("#agentReleaseFile").files[0];
  const notes = $("#agentReleaseNotes").value.trim();
  errorElement.textContent = "";
  if (!file) {
    errorElement.textContent = platform === "linux"
      ? "请选择 Linux x64 OnlineUpdate TAR.GZ。"
      : "请选择 Windows x64 便携版 ZIP。";
    return;
  }
  const expectedSuffix = platform === "linux" ? ".tar.gz" : ".zip";
  if (!file.name.toLowerCase().endsWith(expectedSuffix)) {
    errorElement.textContent = `所选平台的被控端版本必须是 ${expectedSuffix} 文件。`;
    return;
  }
  if (file.size <= 0) {
    errorElement.textContent = "不能上传空文件。";
    return;
  }
  const query = new URLSearchParams({
    version,
    platform,
    architecture: "x86_64",
    file_name: file.name,
    notes,
  });
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = "正在上传并校验…";
  try {
    await api(`/api/agent-releases/upload?${query.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
      timeoutMs: LARGE_UPLOAD_TIMEOUT_MS,
    });
    $("#agentReleaseDialog").close();
    toast(`RigPilot Agent ${version} ${platform === "linux" ? "Linux" : "Windows"} 包已上传并保存；未向设备下发`);
    await refreshAll(true);
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

function syncMinerPackageSource(source = null) {
  const selected = source || $('input[name="minerPackageSource"]:checked')?.value || "upload";
  $$('input[name="minerPackageSource"]').forEach((radio) => { radio.checked = radio.value === selected; });
  const uploadFields = $("#minerPackageUploadFields");
  const importFields = $("#minerPackageImportFields");
  const urlInput = $("#minerPackageUrl");
  uploadFields.classList.toggle("hidden", selected !== "upload");
  importFields.classList.toggle("hidden", selected !== "import");
  urlInput.disabled = selected !== "import";
  urlInput.required = selected === "import";
  syncMinerPackageClassification();
}

function minerPackagePlatformSupported(adapter, osFamily) {
  return (Array.isArray(adapter?.platforms) ? adapter.platforms : [])
    .some((platform) => String(platform?.os_family || "") === osFamily);
}

function minerPackageDualPlatformAvailable() {
  const managedType = $("#minerPackageManagedType").value;
  if (!managedType) return false;
  const source = $('input[name="minerPackageSource"]:checked')?.value || "upload";
  if (source !== "upload") return false;
  const adapter = minerAdapterDefinition(managedType);
  return Boolean(adapter
    && minerPackagePlatformSupported(adapter, "windows")
    && minerPackagePlatformSupported(adapter, "linux"));
}

function minerPackageDualPlatformEnabled() {
  return minerPackageDualPlatformAvailable() && $("#minerPackageDualPlatform")?.checked === true;
}

function syncMinerPackageDualPlatform() {
  const toggle = $("#minerPackageDualPlatform");
  if (!toggle) return;
  const available = minerPackageDualPlatformAvailable();
  if (!available) toggle.checked = false;
  $("#minerPackageDualPlatformField").classList.toggle("hidden", !available);
  const dual = minerPackageDualPlatformEnabled();
  $("#minerPackageManagedOsField").classList.toggle("hidden", dual);
  $("#minerPackageArchiveField").classList.toggle("hidden", dual);
  $("#minerPackageSingleFileField").classList.toggle("hidden", dual);
  const singleFile = $("#minerPackageFile");
  const singleUpload = !dual && $('input[name="minerPackageSource"]:checked')?.value === "upload";
  singleFile.disabled = !singleUpload;
  singleFile.required = singleUpload;
  $("#minerPackageDualFileFields").classList.toggle("hidden", !dual);
  for (const id of ["minerPackageFileWindows", "minerPackageFileLinux"]) {
    const input = $(`#${id}`);
    input.disabled = !dual;
    input.required = dual;
  }
}

function validateMinerPackageFilename(filename, archiveType) {
  const lowerName = String(filename || "").toLowerCase();
  const matches = archiveType === "zip"
    ? lowerName.endsWith(".zip")
    : lowerName.endsWith(".tar.gz") || lowerName.endsWith(".tgz");
  if (!matches) throw new Error(archiveType === "zip" ? "请选择 .zip 文件。" : "请选择 .tar.gz 或 .tgz 文件。");
}

function minerPackageManagedMetadata() {
  const managedType = $("#minerPackageManagedType").value;
  if (!managedType) return null;
  const managedOsFamily = $("#minerPackageManagedOsFamily").value;
  const metadata = {
    managed_miner_type: managedType,
    managed_os_family: managedOsFamily,
    managed_channel: "test",
  };
  if (managedType === "pearlminer" && managedOsFamily === "linux") {
    metadata.managed_variant = $("#minerPackageManagedVariant").value;
  }
  return metadata;
}

function syncMinerPackageClassification({ syncArchive = true } = {}) {
  const nameInput = $("#minerPackageName");
  const metadata = minerPackageManagedMetadata();
  const classified = Boolean(metadata);
  const dual = minerPackageDualPlatformEnabled();
  const standardName = classified ? minerPackageTypeLabel(metadata.managed_miner_type) : "";
  const previousManagedName = nameInput.dataset.managedName || "";
  $("#minerPackageManagedFields").classList.toggle("hidden", !classified);
  $("#minerPackageManagedOsFamily").disabled = !classified || dual;
  $("#minerPackageArchiveType").disabled = dual;
  const showVariant = Boolean(classified && metadata.managed_miner_type === "pearlminer" && (dual || metadata.managed_os_family === "linux"));
  $("#minerPackageManagedVariantField").classList.toggle("hidden", !showVariant);
  $("#minerPackageManagedVariant").disabled = !showVariant;
  $("#minerPackageManagedChannelLabel").value = classified ? "测试版" : "普通自定义包";
  if (classified) {
    nameInput.value = standardName;
    nameInput.readOnly = true;
    nameInput.dataset.managedName = standardName;
    $("#minerPackageNameHint").textContent = `名称由矿工类别固定为 ${standardName}，避免同一软件被拆成多个类别。`;
    if (syncArchive) {
      $("#minerPackageArchiveType").value = metadata.managed_os_family === "windows" ? "zip" : "tar.gz";
    }
  } else {
    if (previousManagedName && nameInput.value === previousManagedName) nameInput.value = "";
    nameInput.readOnly = false;
    delete nameInput.dataset.managedName;
    $("#minerPackageNameHint").textContent = "普通自定义包可自行填写；不会直接作为命令执行。";
  }
  syncMinerPackageDualPlatform();
  renderMinerPackageLimits();
}

async function createMinerPackage() {
  const button = $("#createMinerPackageButton");
  if (button.disabled) return;
  const authEpoch = state.authEpoch;
  const opening = state.minerPackageOpening;
  const current = () => state.authEpoch === authEpoch && state.minerPackageOpening === opening;
  const errorElement = $("#minerPackageError");
  const source = $('input[name="minerPackageSource"]:checked')?.value || "upload";
  const dual = source === "upload" && minerPackageDualPlatformEnabled();
  const metadata = minerPackageManagedMetadata();
  const name = $("#minerPackageName").value.trim();
  const version = $("#minerPackageVersion").value.trim();
  const archiveType = $("#minerPackageArchiveType").value;
  const note = $("#minerPackageNote").value.trim();
  errorElement.textContent = "";
  let dualPlans = null;
  try {
    if (dual) {
      const managedType = $("#minerPackageManagedType").value;
      const windowsFile = $("#minerPackageFileWindows").files[0];
      const linuxFile = $("#minerPackageFileLinux").files[0];
      if (!windowsFile) throw new Error("请选择 Windows .zip 测试包文件。");
      if (!linuxFile) throw new Error("请选择 Linux .tar.gz 或 .tgz 测试包文件。");
      validateMinerPackageFilename(windowsFile.name, "zip");
      validateMinerPackageFilename(linuxFile.name, "tar.gz");
      validateMinerPackageUploadSize(windowsFile, managedType);
      validateMinerPackageUploadSize(linuxFile, managedType);
      dualPlans = [
        { label: "Windows", osFamily: "windows", archive: "zip", file: windowsFile },
        { label: "Linux", osFamily: "linux", archive: "tar.gz", file: linuxFile,
          variant: managedType === "pearlminer" ? $("#minerPackageManagedVariant").value : null },
      ];
    } else {
      if (metadata) {
        const expectedArchive = metadata.managed_os_family === "windows" ? "zip" : "tar.gz";
        if (archiveType !== expectedArchive) {
          throw new Error(`${metadata.managed_os_family === "windows" ? "Windows" : "Linux"} 归类测试包必须使用 ${expectedArchive.toUpperCase()} 格式。`);
        }
      }
      if (source === "upload") {
        const file = $("#minerPackageFile").files[0];
        if (!file) throw new Error("请选择要上传的软件包文件。");
        validateMinerPackageFilename(file.name, archiveType);
        validateMinerPackageUploadSize(file, metadata?.managed_miner_type || "");
      } else {
        const parsedUrl = new URL($("#minerPackageUrl").value.trim());
        if (parsedUrl.protocol !== "https:") throw new Error("导入地址必须使用 HTTPS。");
        if (parsedUrl.username || parsedUrl.password || parsedUrl.hash) throw new Error("导入地址不能包含账号、密码或 # 片段。");
        if (!parsedUrl.pathname || parsedUrl.pathname.endsWith("/")) throw new Error("导入地址必须指向具体的软件包文件。");
        validateMinerPackageFilename(parsedUrl.pathname, archiveType);
      }
    }
  } catch (error) {
    errorElement.textContent = error instanceof TypeError ? "请输入完整有效的 HTTPS 文件地址。" : error.message;
    return;
  }
  button.disabled = true;
  const originalButtonText = button.textContent;
  button.textContent = dual ? "正在上传 1/2（Windows）…" : (source === "upload" ? "正在上传…" : "正在导入…");
  try {
    if (dual) {
      const managedType = $("#minerPackageManagedType").value;
      const results = [];
      for (const [index, plan] of dualPlans.entries()) {
        if (authEpoch !== state.authEpoch) return;
        if (current()) button.textContent = `正在上传 ${index + 1}/2（${plan.label}）…`;
        const query = new URLSearchParams({ name, version, archive_type: plan.archive, note, filename: plan.file.name });
        query.set("managed_miner_type", managedType);
        query.set("managed_os_family", plan.osFamily);
        query.set("managed_channel", "test");
        if (plan.variant != null) query.set("managed_variant", plan.variant);
        try {
          await api(`/api/miner-packages/upload?${query.toString()}`, {
            method: "POST",
            headers: { "Content-Type": "application/octet-stream" },
            body: plan.file,
            timeoutMs: LARGE_UPLOAD_TIMEOUT_MS,
          });
          results.push({ ...plan, ok: true });
        } catch (error) {
          results.push({ ...plan, ok: false, message: error?.message || String(error) });
        }
      }
      if (authEpoch !== state.authEpoch) return;
      const successes = results.filter((item) => item.ok);
      const failures = results.filter((item) => !item.ok);
      if (successes.length) await refreshAll(true);
      if (!current()) return;
      if (!failures.length) {
        $("#minerPackageDialog").close();
        toast(`矿工软件包已上传并登记，已归类到 ${minerPackageTypeLabel(managedType)} 测试版（同版本号 Windows 与 Linux 各一包）`);
      } else if (successes.length) {
        errorElement.textContent = `${successes.map((item) => `${item.label} 包已登记`).join("；")}；${failures.map((item) => `${item.label} 包未登记：${item.message}`).join("；")}。已成功的登记保留有效；修正后可单独重试未登记的平台。`;
      } else {
        errorElement.textContent = `${failures.map((item) => `${item.label} 包未登记：${item.message}`).join("；")}。`;
      }
    } else if (source === "upload") {
      const file = $("#minerPackageFile").files[0];
      const query = new URLSearchParams({ name, version, archive_type: archiveType, note, filename: file.name });
      Object.entries(metadata || {}).forEach(([key, value]) => query.set(key, value));
      await api(`/api/miner-packages/upload?${query.toString()}`, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
        timeoutMs: LARGE_UPLOAD_TIMEOUT_MS,
      });
      if (!current()) return;
      $("#minerPackageDialog").close();
      const category = metadata ? `，已归类到 ${minerPackageTypeLabel(metadata.managed_miner_type)} ${metadata.managed_channel === "custom" ? "自定义包" : "测试版"}` : "";
      toast(`矿工软件包已上传并登记${category}`);
      await refreshAll(true);
    } else {
      await api("/api/miner-packages/import", {
        method: "POST",
        body: {
          name,
          version,
          download_url: $("#minerPackageUrl").value.trim(),
          archive_type: archiveType,
          note,
          ...(metadata || {}),
        },
        timeoutMs: LARGE_UPLOAD_TIMEOUT_MS,
      });
      if (!current()) return;
      $("#minerPackageDialog").close();
      const category = metadata ? `，已归类到 ${minerPackageTypeLabel(metadata.managed_miner_type)} ${metadata.managed_channel === "custom" ? "自定义包" : "测试版"}` : "";
      toast("矿工软件包已从 HTTPS 地址导入" + category);
      await refreshAll(true);
    }
  } catch (error) {
    if (current()) errorElement.textContent = error.message;
  } finally {
    if (current()) {
      button.disabled = false;
      button.textContent = originalButtonText;
    }
  }
}

function normalizeWorkerNameTemplate(value) {
  return String(value || "").replace(/%[0-9a-fA-F]{2}|%[A-Za-z_][A-Za-z0-9_]*%|\{(?:hostname|agent_name)\}/g, (token) =>
    /^(?:%(?:worker_name|work_name)%|\{(?:hostname|agent_name)\})$/i.test(token) ? "%WORKER_NAME%" : token);
}

function validateWorkerNameTemplate(value) {
  const result = normalizeWorkerNameTemplate(value).trim();
  if (!result || result.length > 512 || /[\x00-\x1f\x7f]/.test(result)
      || /[{}]/.test(result.replace(/\{(?:system_hostname|agent_id)\}/g, ""))) {
    throw new Error("Worker 模板支持 %WORKER_NAME%、{system_hostname} 和 {agent_id}，最多 512 字符；旧名称别名仍兼容。");
  }
  return result;
}

function walletOptions() {
  return state.wallets.map((wallet) => `<option value="${escapeHtml(wallet.id)}" data-coin="${escapeHtml(wallet.coin)}">${escapeHtml(wallet.coin)} · ${escapeHtml(wallet.name)}</option>`).join("");
}

function setSecondaryEnabled(enabled) {
  const checkbox = $("#flightSecondaryEnabled");
  const fields = $("#flightSecondaryFields");
  checkbox.checked = enabled;
  checkbox.setAttribute("aria-expanded", String(enabled));
  fields.disabled = !enabled;
  fields.classList.toggle("hidden", !enabled);
  const count = $("#flightEditorJobCount");
  if (count) count.textContent = enabled ? "2 个挖矿任务" : "1 个挖矿任务";
  if (!enabled && state.flightMinerConfigPrefix === "flightSecondary") closeFlightMinerConfig({ restoreFocus: false });
}

function flightMinerConfigIsOpen() {
  return Boolean(state.flightMinerConfigPrefix && $("#flightForm")?.classList.contains("flight-miner-config-open"));
}


function closeFlightMinerConfig({ restoreFocus = true } = {}) {
  const form = $("#flightForm");
  const prefix = state.flightMinerConfigPrefix;
  form?.classList.remove("flight-miner-config-open");
  $$(".flight-editor-job.is-config-target", form).forEach((field) => field.classList.remove("is-config-target"));
  if (form) form.scrollTop = prefix ? state.flightEditorReturnTop : 0;
  state.flightMinerConfigPrefix = null;
  state.flightEditorReturnTop = 0;
  if (restoreFocus && prefix && $("#flightDialog")?.open) {
    form?.querySelector(`[data-flight-configure="${prefix}"]`)?.focus({ preventScroll: true });
  }
}

function openFlightMinerConfig(prefix) {
  const form = $("#flightForm");
  const fieldset = prefix === "flightSecondary" ? $("#flightSecondaryFields") : $("#flightPrimaryFields");
  if (!form || !fieldset || fieldset.disabled || state.flightHistoricalReadOnly) return;
  if (state.flightMinerConfigPrefix === prefix) return;
  if (state.flightMinerConfigPrefix) closeFlightMinerConfig({ restoreFocus: false });
  state.flightEditorReturnTop = form.scrollTop;
  state.flightMinerConfigPrefix = prefix;
  fieldset.classList.add("is-config-target");
  form.classList.add("flight-miner-config-open");
  const details = $(prefix === "flightSecondary" ? "#flightSecondaryAdvanced" : "#flightAdvanced");
  if (details) details.open = true;
  form.scrollTop = 0;
  const firstControl = fieldset.querySelector(
    ".flight-miner-config-only:not(.hidden) :is(input, select, textarea, button):not(:disabled)",
  );
  firstControl?.focus({ preventScroll: true });
}

function reportFlightInvalidControl(control) {
  if (!control) return;
  const fieldset = control.closest("[data-flight-config-fieldset]");
  if (fieldset && control.closest(".flight-miner-config-only")) {
    openFlightMinerConfig(fieldset.dataset.flightConfigFieldset);
  }
  requestAnimationFrame(() => {
    control.reportValidity();
    control.focus({ preventScroll: true });
    control.scrollIntoView({ block: "center", inline: "nearest" });
  });
}

function validateFlightFormForSave(form) {
  if (form.checkValidity()) return true;
  reportFlightInvalidControl(form.querySelector(":invalid"));
  return false;
}

function validateActiveFlightMinerConfig(form) {
  const invalid = form.querySelector(
    ".is-config-target .flight-miner-config-only:invalid, "
    + ".is-config-target .flight-miner-config-only :invalid",
  );
  if (!invalid) return true;
  reportFlightInvalidControl(invalid);
  return false;
}

function resetActiveFlightMinerConfig() {
  const prefix = state.flightMinerConfigPrefix;
  if (!prefix) return;
  const elements = managedLegElements(prefix);
  setManagedExtra(prefix);
  if (elements.executionMode) elements.executionMode.value = "native";
  if (elements.devices) elements.devices.value = "";
  const password = $(`#${prefix}PoolPassword`);
  if (password) password.value = "x";
  syncManagedLeg(prefix, { resetVersion: true });
  if (prefix === "flight") {
    $("#flightGpuOcRows").replaceChildren();
    syncSecondaryMinerVersion();
  } else if (usesIntegratedSrbDual()) {
    syncSecondaryMinerVersion();
  }
  syncFlightGpuOcVisibility();
  const details = $(prefix === "flightSecondary" ? "#flightSecondaryAdvanced" : "#flightAdvanced");
  if (details) details.open = true;
  toast("已恢复当前挖矿软件的默认参数");
}

// Keep the audited built-in miner experience available before authentication,
// during a controller upgrade, and whenever the adapter catalog request fails.
// A catalog response refreshes metadata for identities in this build's trusted
// registry; remote-only identities remain unsupported until a controller update.
// Generated from the same trusted registry as /api/miner-adapters.
const fallbackMinerAdapters = (() => {
  try {
    const data = typeof document !== "undefined" ? document.querySelector("#minerAdapterBootstrap")?.textContent : "";
    const parsed = JSON.parse(data || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch { return []; }
})();

const legacyMinerAdapterOrder = new Map([
  ["xmrig", 0], ["srbminer", 1], ["pearlminer", 2],
]);

const algorithmDisplayNames = Object.freeze({
  "blocknet-argon2id": "Argon2id (BNT)",
  dynex: "Dynex",
  xelishashv3: "XelisHashV3",
  cryptix_ox8: "Cryptix OX8",
  zil: "ZIL",
  quantus: "Quantus",
  randomx: "RandomX",
  pearlhash: "PearlHash",
  etchash: "Etchash",
  ethash: "Ethash",
  kawpow: "KawPow",
  autolykos2: "Autolykos2",
  fishhash: "FishHash",
  qhash: "QHash",
  cryptix: "Cryptix",
  btx: "BTX",
  xelis: "Xelis",
  heavyhash: "HeavyHash",
  blake3_decred: "BLAKE3 Decred",
  karlsenhashv2: "KarlsenHashV2",
  walahash: "WalaHash",
  xhash: "XHash",
  aleph: "ALEPH",
  "beam-iii": "BEAM-III",
  c29ae: "C29AE",
  c30ctx: "C30CTX",
  c31: "C31",
  c32: "C32",
  cr29: "CR29",
  equi144_5: "EQUI144_5",
  equi144_5_excc: "EQUI144_5_EXCC",
  equi192_7: "EQUI192_7",
  equi210_9: "EQUI210_9",
  ethashb3: "ETHASHB3",
  flux: "FLUX",
  gram: "GRAM",
  karlsenv2: "KARLSENV2",
  nexa: "NEXA",
  octopus: "OCTOPUS",
  pyrin: "PYRIN",
  pyrinv2: "PYRINV2",
  radiant: "RADIANT",
  sha3x: "SHA3X",
  ubqhash: "UBQHASH",
});

function availableMinerAdapters() {
  const retiredIdentities = new Set(Object.keys(RETIRED_MINER_LABELS));
  const available = (item) => item && typeof item === "object" && String(item.miner_type || "").trim()
    && ![item.miner_type, item.adapter_id].some((identity) => retiredIdentities.has(String(identity || "").trim().toLowerCase()));
  const fallback = fallbackMinerAdapters.filter(available);
  const fallbackTypes = new Set(fallback.map((item) => String(item.miner_type).trim().toLowerCase()));
  const remote = Array.isArray(state.minerAdapters) ? state.minerAdapters.filter((item) => available(item)
    && fallbackTypes.has(String(item.miner_type).trim().toLowerCase())) : [];
  if (!remote.length) return [...fallback];
  const byType = new Map(remote.map((item) => [String(item.miner_type).trim().toLowerCase(), item]));
  const merged = fallback.map((fallback) => ({
    ...fallback,
    ...(byType.get(String(fallback.miner_type).trim().toLowerCase()) || {}),
    miner_type: fallback.miner_type,
    adapter_id: fallback.adapter_id,
  }));
  return merged.sort((left, right) => {
    const leftType = String(left.miner_type || "").trim().toLowerCase();
    const rightType = String(right.miner_type || "").trim().toLowerCase();
    const leftOrder = legacyMinerAdapterOrder.get(leftType) ?? Number.MAX_SAFE_INTEGER;
    const rightOrder = legacyMinerAdapterOrder.get(rightType) ?? Number.MAX_SAFE_INTEGER;
    return leftOrder - rightOrder || String(left.display_name || leftType).localeCompare(String(right.display_name || rightType), "zh-CN");
  });
}

function minerAdapterDefinition(minerType) {
  const normalized = String(minerType || "").trim().toLowerCase();
  return availableMinerAdapters().find((item) => String(item.miner_type || "").trim().toLowerCase() === normalized) || null;
}

function minerAdapterOptionLabel(adapter) {
  const minerType = String(adapter?.miner_type || "").trim().toLowerCase();
  if (minerType === "xmrig") return "XMRig（XMR / CPU 推荐）";
  if (minerType === "srbminer") return "SRBMiner-MULTI（CPU / GPU）";
  if (minerType === "pearlminer") return "Pearl Miner（PEARL / GPU）";
  const resources = Array.isArray(adapter?.resource_kinds)
    ? adapter.resource_kinds.map((item) => String(item).toUpperCase()).filter(Boolean)
    : [];
  return `${String(adapter?.display_name || minerType || "未知矿工")}${resources.length ? `（${resources.join(" / ")}）` : ""}`;
}

function populateManagedMinerTypeSelect(prefix, selectedType = "") {
  const select = $(`#${prefix}MinerType`);
  if (!select) return;
  const current = String(selectedType || select.value || "").trim().toLowerCase();
  const adapters = availableMinerAdapters();
  select.innerHTML = adapters.map((adapter) => {
    const minerType = String(adapter.miner_type || "").trim().toLowerCase();
    return `<option value="${escapeHtml(minerType)}" data-adapter-id="${escapeHtml(adapter.adapter_id || "")}">${escapeHtml(minerAdapterOptionLabel(adapter))}</option>`;
  }).join("");
  if (current && !adapters.some((item) => String(item.miner_type).trim().toLowerCase() === current)) {
    select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(current)}" data-history-adapter="true">${escapeHtml(RETIRED_MINER_LABELS[current] || current)} · 已退休 / 历史适配器</option>`);
  }
  const preferred = current
    || (prefix === "flightSecondary" && minerAdapterDefinition("pearlminer") ? "pearlminer" : "")
    || (minerAdapterDefinition("xmrig") ? "xmrig" : "")
    || String(adapters[0]?.miner_type || "");
  select.value = preferred;
}

function minerAdapterAlgorithms(minerType) {
  const raw = minerAdapterDefinition(minerType)?.algorithms;
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  return raw.map((item) => {
    const source = item && typeof item === "object" ? item : { name: item };
    const name = String(source.name || "").trim().toLowerCase();
    return { ...source, name };
  }).filter((item) => item.name && !seen.has(item.name) && seen.add(item.name));
}

function minerAdapterDefaultAlgorithm(minerType, coin = "") {
  const adapter = minerAdapterDefinition(minerType);
  if (!adapter) return "";
  const coinDefaults = adapter.coin_defaults && typeof adapter.coin_defaults === "object"
    ? adapter.coin_defaults
    : {};
  return String(coinDefaults[String(coin || "").trim().toUpperCase()] || adapter.default_algorithm || "").trim().toLowerCase();
}

function minerAdapterHasCapability(minerType, capability) {
  const capabilities = minerAdapterDefinition(minerType)?.capabilities;
  return Array.isArray(capabilities) && capabilities.includes(capability);
}

function minerAdapterOptionNames(minerType) {
  const options = minerAdapterDefinition(minerType)?.options;
  return new Set(Array.isArray(options) ? options.map((option) => String(option?.name || "")).filter(Boolean) : []);
}

function minerAdapterOptionSpec(minerType, name) {
  const options = minerAdapterDefinition(minerType)?.options;
  return Array.isArray(options) ? options.find((option) => option?.name === name) || null : null;
}

function minerAdapterAlgorithmSpec(minerType, algorithm) {
  const name = String(algorithm || "").trim().toLowerCase();
  return minerAdapterAlgorithms(minerType).find((item) => item.name === name || (item.aliases || []).includes(name)) || null;
}

function minerAdapterAlgorithmResource(minerType, algorithm, selectedResource = "") {
  const spec = minerAdapterAlgorithmSpec(minerType, algorithm);
  return String(selectedResource || spec?.resource_kind || "").toLowerCase();
}

function algorithmVersionReason(spec, version) {
  if (!spec || !version) return "";
  const parse = (value) => /^\d+\.\d+\.\d+$/.test(String(value || "")) ? String(value).split(".").map(Number) : null;
  const chosen = parse(version);
  if (!chosen) return "";
  const compare = (left, right) => left[0] - right[0] || left[1] - right[1] || left[2] - right[2];
  const minimum = parse(spec.minimum_version), removed = parse(spec.removed_version);
  if (minimum && compare(chosen, minimum) < 0) return `${spec.name} 需要矿工 ${spec.minimum_version} 或更新版本`;
  if (removed && compare(chosen, removed) >= 0) return `${spec.name} 已在矿工 ${spec.removed_version} 移除；历史记录保留，请选择兼容旧版本`;
  return "";
}

function managedLegResource(prefix) {
  const elements = managedLegElements(prefix);
  return minerAdapterAlgorithmResource(elements.type.value, effectiveFlightAlgorithm(prefix), adapterSchemaOptionValue(prefix, "resource_kind", ""));
}

function managedLegProtocolReason(prefix, { requirePin = true } = {}) {
  const elements = managedLegElements(prefix), type = elements.type.value;
  const algorithm = effectiveFlightAlgorithm(prefix), adapter = minerAdapterDefinition(type);
  const spec = minerAdapterAlgorithmSpec(type, algorithm);
  const versionReason = algorithmVersionReason(spec, elements.version.value);
  if (versionReason) return versionReason;
  const resources = spec?.supported_resources || (spec?.resource_kind ? [spec.resource_kind] : []);
  const resource = managedLegResource(prefix);
  if (resources.length && !resources.includes(resource)) return "所选算法不支持此任务资源";
  const coin = String(elements.coin.value || "").toUpperCase();
  if (spec?.coins?.length && coin && !spec.coins.includes(coin)) return "所选算法与当前钱包币种不兼容";
  const url = $(`#${prefix}Pool`)?.value.trim() || "";
  const scheme = url.match(/^([a-z][a-z0-9+.-]*):\/\//i)?.[1]?.toLowerCase();
  if (!scheme) return "";
  if (adapter?.pool_schemes?.length && !adapter.pool_schemes.includes(scheme)) return "当前矿工不支持此矿池协议";
  const rule = adapter?.protocol_requirements?.[scheme];
  if (!rule) return "";
  if (rule.algorithms?.length && !rule.algorithms.includes(spec?.name || algorithm)) return "此矿池协议不适用于所选算法";
  if (requirePin) for (const name of rule.required_options || []) {
    const value = adapterSchemaOptionValue(prefix, name, "");
    if (typeof value !== "string" || !value.trim()) return "Quantus QUIC 需要节点提供的 DER 证书 SHA256 pin";
  }
  if (requirePin && rule.wallet_mode_option && adapterSchemaOptionValue(prefix, rule.wallet_mode_option, rule.default_wallet_mode) === "token" && !$(`#${prefix}PoolPassword`)?.value.trim()) return "私有节点 QUIC 需要输入节点提供的私密认证 token";
  return "";
}

function syncManagedProtocolUI(prefix) {
  const elements = managedLegElements(prefix), adapter = minerAdapterDefinition(elements.type.value);
  const spec = minerAdapterAlgorithmSpec(elements.type.value, effectiveFlightAlgorithm(prefix));
  const pool = $(`#${prefix}Pool`), scheme = pool?.value.match(/^([a-z][a-z0-9+.-]*):\/\//i)?.[1]?.toLowerCase();
  const quic = elements.type.value === "srbminer" && spec?.name === "quantus" && scheme === "quic";
  const nodeToken = quic && adapterSchemaOptionValue(prefix, "quic_wallet_mode", "wallet") === "token";
  const reason = managedLegProtocolReason(prefix);
  if (pool) pool.setCustomValidity(reason);
  const pin = $('[data-adapter-option="tls_cert_sha256"]', adapterOptionContainer(prefix));
  if (pin) pin.required = quic;
  const mode = '[data-adapter-option="quic_wallet_mode"]';
  const modeSelect = $(mode, adapterOptionContainer(prefix));
  if (modeSelect?.options[0]) modeSelect.options[0].textContent = "公共矿池：付款钱包（默认）";
  const resource = $('[data-adapter-option="resource_kind"]', adapterOptionContainer(prefix));
  if (resource) {
    const allowed = spec?.supported_resources || [spec?.resource_kind].filter(Boolean);
    [...resource.options].forEach((option) => { if (option.value) option.disabled = !allowed.includes(option.value); });
    if (resource.options[0]) resource.options[0].textContent = `算法默认（${String(spec?.resource_kind || "").toUpperCase()}）`;
  }
  if (elements.type.value === "srbminer") {
    $(`#${prefix}PoolLabelText`).textContent = spec?.name === "quantus" ? "Quantus Stratum / QUIC 地址" : "SRBMiner Stratum 矿池地址";
    $(`#${prefix}PoolHint`).textContent = spec?.name === "quantus"
      ? "Quantus 3.6.4+ 可使用 Stratum 或 QUIC 地址。QUIC 需要证书 pin；公共矿池使用付款钱包，私有节点请在矿工配置中明确选择 token 身份。Intel ARC 需要 3.7.0+，AMD RX4xx/RX5xx 需要 3.6.8+。"
      : "保持所选矿工支持的协议与端口；资源按实际任务选择，不同时占用 CPU 和 GPU。";
    const field = $(`#${prefix}PoolPasswordField`);
    if (field?.firstChild?.nodeType === 3) field.firstChild.textContent = nodeToken ? "私有节点 QUIC 认证 token" : "矿池密码";
    const hint = field?.querySelector("small");
    if (hint) hint.textContent = nodeToken ? "输入节点提供的 token；此私密值用于节点认证，付款钱包仍单独保存。" : (quic ? "公共 QUIC 矿池使用所选付款钱包；此密码不作为连接身份。" : "多数 Stratum 矿池使用 x；它不是控制端或系统密码。");
  }
}

function srbDualPairSupported(primary, secondary, primaryResource, secondaryResource) {
  const adapter = minerAdapterDefinition("srbminer");
  if (primary === secondary && primary === "randomx") return false;
  if ((adapter?.dual_any_secondary || []).includes(primary)) return true;
  if ([primaryResource, secondaryResource].includes("cpu") && [primaryResource, secondaryResource].includes("gpu")) return true;
  return (adapter?.dual_algorithm_pairs || []).some((pair) => pair[0] === primary && pair[1] === secondary);
}

function populateManagedAlgorithmSelect(prefix, selectedAlgorithm = "") {
  const elements = managedLegElements(prefix);
  if (!elements.algorithm) return;
  const current = String(selectedAlgorithm || elements.algorithm.value || "").trim().toLowerCase();
  const algorithms = minerAdapterAlgorithms(elements.type.value).filter((item) => !algorithmVersionReason(item, elements.version.value));
  elements.algorithm.innerHTML = '<option value="">自动</option>' + algorithms.map((algorithm) => (
    `<option value="${escapeHtml(algorithm.name)}">${escapeHtml(algorithmDisplayNames[algorithm.name] || algorithm.name)}</option>`
  )).join("");
  if (current && !algorithms.some((algorithm) => algorithm.name === current)) {
    elements.algorithm.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(current)}" data-history-algorithm="true">${escapeHtml(current)} · 历史 / 当前版本不可用</option>`);
  }
  elements.algorithm.value = current;
}

// These options already have richer, compatibility-tested controls elsewhere
// in the form.  All remaining job-scoped options are rendered from the
// adapter schema so adding a bounded typed option no longer needs bespoke UI.
const specializedAdapterOptions = new Set([
  "cpu_threads", "cpu_max_threads_hint", "huge_pages", "gpu_backend",
  "cuda_variant", "tls", "extra_args",
]);

const cpuRandomxAdapterOptions = new Set([
  "randomx_1gb_pages", "pause_on_battery", "cpu_threads_reduce", "cpu_threads_intensity",
]);

const adapterOptionPresentations = Object.freeze({
  cpu_profile: ["CPU 性能策略", "balanced 为平衡，throughput 为吞吐优先，efficiency 为节能优先；线程留空时由矿工按所选策略与 CPU、内存自动选择。"],
  randomx_1gb_pages: ["启用 RandomX 1GB Pages", "默认关闭；仅在受支持的 Linux 原生、CPU、内存与权限环境中生效，Agent 不会代替系统配置大页。"],
  pause_on_battery: ["使用电池时暂停（XMRig）", "笔记本切换到电池供电时暂停 CPU 挖矿。"],
  cpu_threads_reduce: ["减少 CPU 线程数（SRBMiner）", "在自动线程数基础上减少指定数量；与显式 CPU 线程数不要同时填写。"],
  cpu_threads_intensity: ["CPU 线程强度（SRBMiner）", "可选 1、2 或 4；保持自动通常最稳妥。"],
  keepalive: ["保持矿池连接", "保持 Stratum 连接，减少空闲断开。"],
  extended_log: ["扩展日志（SRBMiner）", "输出更多矿工诊断信息；仅排障时开启。"],
});

function adapterOptionContainer(prefix) {
  return $(`#${prefix}AdapterOptions`);
}

function adapterSchemaOptionValue(prefix, name, fallback = undefined) {
  const container = adapterOptionContainer(prefix);
  if (!container) return fallback;
  try {
    const stored = JSON.parse(container.dataset.optionValues || "{}");
    return Object.hasOwn(stored, name) ? stored[name] : fallback;
  } catch (_error) {
    return fallback;
  }
}

function setAdapterSchemaExtraValues(prefix, extra = {}) {
  const container = adapterOptionContainer(prefix);
  if (!container) return;
  container.dataset.optionValues = JSON.stringify(extra && typeof extra === "object" ? extra : {});
}

function resetAdapterSchemaOptionValues(prefix, extra = {}) {
  const container = adapterOptionContainer(prefix);
  if (!container) return;
  container.innerHTML = "";
  container.classList.add("hidden");
  setAdapterSchemaExtraValues(prefix, extra);
}

function adapterOptionDefault(option) {
  if (option.name === "keepalive") return true; // Preserve the current managed-flight default.
  if (option.default !== undefined && option.default !== null) return option.default;
  return option.type === "boolean" ? false : "";
}

function preserveRenderedAdapterOptionValues(prefix) {
  const container = adapterOptionContainer(prefix);
  if (!container?.children.length) return;
  let values = {};
  try {
    values = JSON.parse(container.dataset.optionValues || "{}");
  } catch (_error) {
    values = {};
  }
  $$('[data-adapter-option]', container).forEach((input) => {
    const name = input.dataset.adapterOption;
    if (input.dataset.optionType === "boolean") values[name] = input.checked;
    else if (input.value === "") delete values[name];
    else if (input.dataset.optionType === "integer") values[name] = Number(input.value);
    else values[name] = input.value;
  });
  container.dataset.optionValues = JSON.stringify(values);
}

function renderAdapterOptionControls(prefix) {
  const container = adapterOptionContainer(prefix);
  if (!container) return;
  preserveRenderedAdapterOptionValues(prefix);
  const usesCpuRandomx = managedLegUsesCpuRandomX(prefix);
  const adapter = minerAdapterDefinition(managedLegElements(prefix).type.value);
  const options = Array.isArray(adapter?.options)
    ? adapter.options.filter((option) => (
      option && typeof option === "object"
      && (option.scope || "job") === "job"
      && !specializedAdapterOptions.has(String(option.name || ""))
      && (usesCpuRandomx || !cpuRandomxAdapterOptions.has(String(option.name || "")))
    ))
    : [];
  options.sort((left, right) => Number(cpuRandomxAdapterOptions.has(String(right.name || ""))) - Number(cpuRandomxAdapterOptions.has(String(left.name || ""))));
  let cpuHeadingRendered = false;
  container.innerHTML = options.map((option) => {
    const name = String(option.name || "");
    const value = adapterSchemaOptionValue(prefix, name, adapterOptionDefault(option));
    const presentation = adapterOptionPresentations[name];
    const label = String(presentation?.[0] || option.label || name);
    const description = String(presentation?.[1] || option.description || "由矿工适配器定义");
    const heading = cpuRandomxAdapterOptions.has(name) && !cpuHeadingRendered ? (cpuHeadingRendered = true, '<div class="adapter-option-group-heading"><strong>CPU 扩展参数</strong><small>Huge Pages 与 1GB Pages 是独立选项，可分别启用</small></div>') : "";
    if (option.type === "boolean") {
      return `${heading}<label class="toggle-field compact"><input type="checkbox" data-adapter-option="${escapeHtml(name)}" data-option-type="boolean"${value === true ? " checked" : ""}><span><strong>${escapeHtml(label)}</strong><small class="field-hint">${escapeHtml(description)}</small></span></label>`;
    }
    if (option.type === "choice" && Array.isArray(option.choices)) {
      const choices = option.choices.map((choice) => (
        `<option value="${escapeHtml(choice)}"${String(choice) === String(value) ? " selected" : ""}>${escapeHtml(name === "quic_wallet_mode" ? (choice === "token" ? "私有节点：认证 token" : "公共矿池：付款钱包") : choice)}</option>`
      )).join("");
      return `${heading}<label>${escapeHtml(label)}<select data-adapter-option="${escapeHtml(name)}" data-option-type="choice"><option value="">自动</option>${choices}</select><small class="field-hint">${escapeHtml(description)}</small></label>`;
    }
    if (option.type === "string") {
      const rendered = value === undefined || value === null ? "" : value;
      const maximum = Number.isInteger(option.max_length) ? ` maxlength="${option.max_length}"` : "";
      return `${heading}<label>${escapeHtml(label)}<input type="text"${maximum} spellcheck="false" value="${escapeHtml(rendered)}" data-adapter-option="${escapeHtml(name)}" data-option-type="string"><small class="field-hint">${escapeHtml(description)}</small></label>`;
    }
    if (option.type === "integer") {
      const minimum = Number.isInteger(option.minimum) ? ` min="${option.minimum}"` : "";
      const maximum = Number.isInteger(option.maximum) ? ` max="${option.maximum}"` : "";
      const rendered = value === undefined || value === null ? "" : value;
      return `${heading}<label>${escapeHtml(label)}<input type="number" step="1"${minimum}${maximum} value="${escapeHtml(rendered)}" data-adapter-option="${escapeHtml(name)}" data-option-type="integer" placeholder="自动"><small class="field-hint">${escapeHtml(description)}</small></label>`;
    }
    return "";
  }).join("");
  container.classList.toggle("hidden", !container.children.length);
}

function collectAdapterSchemaOptions(prefix) {
  const adapter = minerAdapterDefinition(managedLegElements(prefix).type.value);
  const usesCpuRandomx = managedLegUsesCpuRandomX(prefix);
  const specifications = new Map(
    (Array.isArray(adapter?.options) ? adapter.options : []).map((option) => [String(option.name || ""), option]),
  );
  const result = {};
  $$('[data-adapter-option]', adapterOptionContainer(prefix)).forEach((input) => {
    const name = input.dataset.adapterOption;
    if (!usesCpuRandomx && cpuRandomxAdapterOptions.has(name)) return;
    const specification = specifications.get(name) || {};
    if (input.dataset.optionType === "boolean") {
      result[name] = input.checked;
      return;
    }
    if (input.value === "") return;
    if (input.dataset.optionType === "integer") {
      result[name] = Number(input.value);
      return;
    }
    if (input.dataset.optionType === "string") { result[name] = input.value; return; }
    const numericChoice = Array.isArray(specification.choices)
      && specification.choices.find((choice) => typeof choice === "number" && String(choice) === input.value);
    result[name] = numericChoice === undefined ? input.value : numericChoice;
  });
  setAdapterSchemaExtraValues(prefix, result);
  return result;
}

function flightSecondaryMode() {
  return $("#flightSecondaryMode")?.value === "integrated" ? "integrated" : "independent";
}

function usesIntegratedSrbDual() {
  return $("#flightSecondaryEnabled").checked && flightSecondaryMode() === "integrated";
}

function syncFlightCoin(walletSelector, coinSelector) {
  const selected = $(walletSelector).selectedOptions[0];
  if (selected?.dataset.coin) $(coinSelector).value = selected.dataset.coin;
}

function managedLegElements(prefix) {
  return {
    type: $(`#${prefix}MinerType`),
    version: $(`#${prefix}MinerVersion`),
    wallet: $(`#${prefix}Wallet`),
    coin: $(`#${prefix}Coin`),
    algorithm: $(`#${prefix}Algorithm`),
    devices: $(`#${prefix}DeviceIndices`),
    cpuHint: $(`#${prefix}CpuHint`),
    cpuThreads: $(`#${prefix}CpuThreads`),
    hugePages: $(`#${prefix}HugePages`),
    executionMode: $(`#${prefix}ExecutionMode`),
    gpuBackend: prefix === "flight" ? $("#flightGpuBackend") : $("#flightSecondaryGpuBackend"),
    cudaVariant: prefix === "flight" ? $("#flightCudaVariant") : $("#flightSecondaryCudaVariant"),
    extraArgs: $(`#${prefix}ExtraArgs`),
  };
}

function effectiveFlightAlgorithm(prefix) {
  const elements = managedLegElements(prefix);
  const coin = elements.coin.value.toUpperCase();
  return String(
    elements.algorithm.value
    || minerAdapterDefaultAlgorithm(elements.type.value, coin)
    || "",
  ).toLowerCase();
}

function managedLegUsesCpuRandomX(prefix) {
  return managedLegElements(prefix).type.value === "xmrig" || managedLegResource(prefix) === "cpu";
}

function syncCpuRandomxFields(prefix) {
  const elements = managedLegElements(prefix);
  const minerType = elements.type.value;
  const adapterOptions = minerAdapterOptionNames(minerType);
  const usesCpuRandomx = managedLegUsesCpuRandomX(prefix);
  const suffix = prefix === "flight" ? "" : "Secondary";
  $(`#flight${suffix}CpuRandomxSection`)?.classList.toggle("hidden", !usesCpuRandomx);
  $(`#flight${suffix}CpuHintField`)?.classList.toggle("hidden", !usesCpuRandomx || !adapterOptions.has("cpu_max_threads_hint"));
  $(`#flight${suffix}CpuThreadsField`)?.classList.toggle("hidden", !usesCpuRandomx || !adapterOptions.has("cpu_threads"));
  $(`#flight${suffix}HugePagesField`)?.classList.toggle("hidden", !usesCpuRandomx || !adapterOptions.has("huge_pages"));
  elements.hugePages.disabled = !usesCpuRandomx || !adapterOptions.has("huge_pages");
  elements.cpuHint.disabled = !usesCpuRandomx || !adapterOptions.has("cpu_max_threads_hint");
  elements.cpuThreads.disabled = !usesCpuRandomx || !adapterOptions.has("cpu_threads");
  const threadSpec = minerAdapterOptionSpec(minerType, "cpu_threads");
  const schemaMaximum = Number.isInteger(threadSpec?.maximum) && threadSpec.maximum >= 1 ? threadSpec.maximum : null;
  const maximum = minerType === "xmrig" ? Math.min(schemaMaximum ?? 256, 256) : schemaMaximum;
  elements.cpuThreads.max = maximum === null ? "" : String(maximum);
  const legacyCpu = minerType === "xmrig" || minerType === "srbminer";
  const heading = $(`#flight${suffix}CpuRandomxHeading`);
  const hint = $(`#flight${suffix}CpuRandomxHint`);
  const threadsHint = $(`#flight${suffix}CpuThreadsHint`);
  if (heading) heading.textContent = legacyCpu ? "CPU / RandomX" : "CPU 挖矿参数";
  if (hint) hint.textContent = legacyCpu
    ? (prefix === "flight" ? "仅在 XMRig 或 SRBMiner RandomX 挖矿时使用" : "第二矿工使用 XMRig 或 SRBMiner RandomX 时生效")
    : "仅在所选算法使用 CPU 时生效；各字段按当前矿工适配器说明设置。";
  if (threadsHint) threadsHint.textContent = legacyCpu
    ? (prefix === "flight" ? "留空为自动；XMRig 界面上限 256，SRBMiner 上限 1024，通常不要超过逻辑核心数。" : "留空为自动；XMRig 界面上限 256，SRBMiner 上限 1024。")
    : (String(threadSpec?.description || "").trim() || "留空由矿工自动选择线程数；按实际 CPU 和内存资源设置。");
  if (!usesCpuRandomx || !adapterOptions.has("cpu_threads")) elements.cpuThreads.value = "";
  if (!usesCpuRandomx || !adapterOptions.has("cpu_max_threads_hint")) elements.cpuHint.value = "100";
  renderAdapterOptionControls(prefix);
  return usesCpuRandomx;
}

function managedLegSupportsWsl(prefix) {
  const elements = managedLegElements(prefix);
  if (!elements.executionMode || usesIntegratedSrbDual()) return false;
  return minerAdapterHasCapability(elements.type.value, "wsl_gpu")
    && managedLegResource(prefix) === "gpu";
}

function syncManagedExecutionMode(prefix) {
  const elements = managedLegElements(prefix);
  const field = $(`#${prefix}ExecutionModeField`);
  const hint = $(`#${prefix}ExecutionModeHint`);
  if (!elements.executionMode || !field) return;
  const supported = managedLegSupportsWsl(prefix);
  if (!supported) elements.executionMode.value = "native";
  elements.executionMode.disabled = !supported;
  field.classList.remove("hidden");
  const usesWsl = supported && elements.executionMode.value === "wsl";
  if (usesWsl && elements.type.value === "pearlminer" && elements.gpuBackend) {
    elements.gpuBackend.value = "nvidia";
  }
  if (hint) {
    hint.textContent = usesWsl
      ? "将选择 Linux NVIDIA 矿工包并在 Windows WSL2 中运行；目标机没有 WSL、没有 NVIDIA WSL 映射或当前不可用时，下发会明确报错。"
      : "默认直接在设备系统运行；只有 Windows NVIDIA GPU 矿工可以显式改用 WSL2。";
  }
}

function flightGpuOcIsSupported() {
  if ($("#flightMinerType").value !== "srbminer" || $("#flightExecutionMode")?.value === "wsl") return false;
  const primary = managedLegResource("flight");
  if ($("#flightSecondaryEnabled").checked && flightSecondaryMode() === "independent") {
    return primary === "gpu" && managedLegResource("flightSecondary") === "cpu";
  }
  return primary === "gpu" || (usesIntegratedSrbDual() && managedLegResource("flightSecondary") === "gpu");
}

function updateFlightGpuOcIndexHints() {
  $$('[data-flight-gpu-index-hint]', $("#flightGpuOcRows")).forEach((element) => {
    const row = element.closest(".flight-gpu-oc-row");
    element.textContent = row?.dataset.legacyGpuIndex !== undefined
      ? `旧配置：SRBMiner GPU ${row.dataset.legacyGpuIndex}；填写型号即可升级`
      : "例如 5080、5070、5060 Ti；同型号多张卡自动共用本行参数";
  });
}

function syncFlightGpuOcVisibility() {
  const supported = flightGpuOcIsSupported();
  const section = $("#flightGpuOcSection");
  section.classList.toggle("hidden", !supported);
  $$("input, select, textarea, button", section).forEach((control) => {
    control.disabled = !supported;
  });
  updateFlightGpuOcIndexHints();
}

function populateManagedVersionSelect(prefix, selectedVersion = "", resetVersion = false) {
  const elements = managedLegElements(prefix);
  const definition = managedMinerDefinition(elements.type.value);
  const versions = managedMinerVersions(definition);
  const current = resetVersion ? "" : String(selectedVersion || elements.version.value || "");
  const latest = String(definition?.latest_version || definition?.version || versions[0]?.version || "");
  const selected = current || latest || versions[0]?.version || "";
  elements.version.innerHTML = versions.map((version) => (
    `<option value="${escapeHtml(version.version)}">${escapeHtml(managedVersionOptionLabel(version, elements.type.value))}</option>`
  )).join("");
  const found = versions.some((version) => version.version === selected);
  if (selected && !found) {
    elements.version.insertAdjacentHTML(
      "afterbegin",
      `<option value="${escapeHtml(selected)}">${escapeHtml(selected)} · 历史固定版本（目录外）</option>`,
    );
  }
  if (!elements.version.options.length) {
    elements.version.innerHTML = '<option value="">暂无可下发版本</option>';
  }
  elements.version.value = selected;
  elements.version.dataset.catalogMissing = String(Boolean(selected && !found));
  renderManagedVersionCompatibility(prefix);
}

function syncSecondaryMinerVersion() {
  if (!usesIntegratedSrbDual()) {
    $("#flightSecondaryMinerVersion").disabled = false;
    return;
  }
  const primary = $("#flightMinerVersion").value;
  const primaryType = $("#flightMinerType").value;
  if (!primary || !minerAdapterHasCapability(primaryType, "integrated_dual") || $("#flightSecondaryMinerType").value !== primaryType) return;
  populateManagedVersionSelect("flightSecondary", primary, false);
  $("#flightSecondaryMinerVersion").value = primary;
  $("#flightSecondaryMinerVersion").disabled = true;
}

function syncSecondaryMode({ preserveAlgorithm = false, selectedVersion = "", resetVersion = false } = {}) {
  $("#flightSecondaryHint").textContent = "可使用受支持矿工的同进程双挖，或启动第二个独立托管矿工进程。";
  if (!$("#flightSecondaryEnabled").checked) {
    syncManagedExecutionMode("flight");
    renderManagedVersionCompatibility("flight");
    syncManagedExtraArgsHint("flight");
    syncManagedExtraArgsHint("flightSecondary");
    syncFlightGpuOcVisibility();
    return;
  }
  const mode = $("#flightSecondaryMode");
  const integratedOption = [...mode.options].find((option) => option.value === "integrated");
  const primaryType = $("#flightMinerType").value;
  const primaryAdapter = minerAdapterDefinition(primaryType);
  const primaryName = String(primaryAdapter?.display_name || primaryType);
  const supportsIntegrated = minerAdapterHasCapability(primaryType, "integrated_dual");
  if (integratedOption) {
    integratedOption.disabled = !supportsIntegrated;
    integratedOption.textContent = supportsIntegrated ? `${primaryName} 内置双挖（同一进程）` : "内置双挖（当前矿工不支持）";
  }
  if (!supportsIntegrated && mode.value === "integrated") mode.value = "independent";
  const integrated = usesIntegratedSrbDual();
  syncManagedExecutionMode("flight");
  renderManagedVersionCompatibility("flight");
  $("#flightSecondaryModeHint").textContent = integrated
    ? `同一 ${primaryName} 进程运行两个算法；第二矿工软件和版本自动跟随主挖。`
    : "启动第二个独立托管进程；矿工软件、版本、钱包和矿池互不绑定。";
  $("#flightSecondaryMinerVersionHint").textContent = integrated
    ? `同一 ${primaryName} 进程只能使用一个版本，第二矿工自动跟随主挖。`
    : "独立模式固定自己的版本，不跟随主矿工变更。";
  if (integrated) {
    if (!minerAdapterDefinition($("#flightSecondaryMinerType").value)) {
      // A retired second leg must retain its own identity and fixed version
      // until the operator explicitly chooses a current miner for the copy.
      syncManagedLeg("flightSecondary", { preserveAlgorithm, selectedVersion, resetVersion });
      $("#flightSecondaryMinerType").disabled = false;
      $("#flightSecondaryMinerVersion").disabled = false;
      $("#flightSecondaryModeHint").textContent = "第二矿工已退休或当前不可用；创建副本时请明确选择当前矿工，并核对独立 / 内置双挖模式。";
      return;
    }
    $("#flightSecondaryMinerType").value = primaryType;
    selectedVersion = $("#flightMinerVersion").value || selectedVersion;
    resetVersion = false;
  }
  $("#flightSecondaryMinerType").disabled = integrated;
  syncManagedLeg("flightSecondary", { preserveAlgorithm, selectedVersion, resetVersion });
  $("#flightSecondaryMinerType").disabled = integrated;
  $("#flightSecondaryMinerVersion").disabled = integrated;
  syncManagedExtraArgsHint("flight");
  syncManagedExtraArgsHint("flightSecondary");
  syncFlightGpuOcVisibility();
}

function syncManagedExtraArgsHint(prefix) {
  const field = $(`#${prefix}ExtraArgsField`);
  const hint = field?.querySelectorAll?.(".field-hint")?.[1];
  if (!hint) return;
  const integrated = usesIntegratedSrbDual();
  const primaryType = $("#flightMinerType").value;
  const primaryName = minerAdapterDefinition(primaryType)?.display_name || primaryType;
  const modeHint = integrated
    ? (primaryType === "srbminer"
      ? "SRBMiner 内置双挖共用一个进程，按主挖、第二矿工的配置顺序各追加一次额外参数。"
      : `${primaryName} 内置双挖共用一个进程；额外参数按该适配器支持的方式处理。`)
    : "主挖和第二矿工分别保存各自的额外参数。";
  hint.textContent = `非空额外参数需要目标设备运行 Agent 0.8.31 或更新版本，并已上报能力 managed_mining.extra_args.v1。${modeHint}`;
}

function seineFlightPackages(version) {
  return (Array.isArray(state.minerPackages) ? state.minerPackages : []).filter((item) => (
    item && item.managed_miner_type === "seine" && String(item.version || "") === String(version || "")
    && item.managed_channel === "test" && item.source_type === "uploaded" && item.available === true
    && ["linux", "windows"].includes(item.managed_os_family) && (item.managed_variant == null || item.managed_variant === "")
    && typeof item.id === "string" && Boolean(item.id) && /^[a-f0-9]{64}$/i.test(item.sha256 || "")
  ));
}

function seineFlightPackageLabel(minerPackage) {
  const platform = {linux: "Linux", windows: "Windows"}[minerPackage.managed_os_family] || "平台未声明";
  return `上传构建（非官方） · ${platform} · ${minerPackage.name || minerPackage.id} ${minerPackage.version || ""} · SHA256 ${String(minerPackage.sha256 || "").slice(0, 12)}`;
}

function populateSeineFlightPackageSelect(prefix, selectedId) {
  const select = $(`#${prefix}NativePackageId`);
  const field = $(`#${prefix}NativePackageField`);
  if (!select || !field) return;
  const elements = managedLegElements(prefix);
  const minerType = elements.type.value;
  const version = elements.version.value;
  const sameIdentity = select.dataset.minerType === minerType && select.dataset.minerVersion === version;
  const selected = minerType === "seine" ? String(selectedId !== undefined ? (selectedId || "") : (sameIdentity ? select.value : "")) : "";
  const packages = minerType === "seine" ? seineFlightPackages(version) : [];
  const missing = Boolean(selected && !packages.some((item) => item.id === selected));
  const automaticLabel = packages.length
    ? "按目标平台自动选择（优先上传构建组）"
    : "自动选择官方包";
  select.innerHTML = `<option value="">${automaticLabel}</option>` + packages.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(seineFlightPackageLabel(item))}</option>`
  )).join("") + (missing ? `<option value="${escapeHtml(selected)}">绑定上传构建不可用 · ${escapeHtml(selected)}</option>` : "");
  select.value = selected;
  select.dataset.minerType = minerType;
  select.dataset.minerVersion = version;
  select.dataset.packageMissing = String(missing);
  select.disabled = minerType !== "seine" || Boolean(state.flightHistoricalReadOnly);
  field.classList.toggle("hidden", minerType !== "seine");
  const hint = $(`#${prefix}NativePackageHint`);
  if (hint) {
    const automatic = packages.length
      ? "留空时按目标设备系统选择该版本已上传且通过 SHA256 校验的构建；缺少对应平台时不会自动改用官方包。"
      : "";
    const compatibility = minerType === "seine" && ["0.2.15", "0.2.44", "0.2.45"].includes(version)
      ? `${version} 官方 Linux 包需要更高 GLIBC；Ubuntu 22.04（GLIBC 2.35）请仅选择已验证的同版本兼容构建。上传构建为非官方包，按平台和 SHA256 区分。`
      : "";
    hint.textContent = automatic || compatibility
      || "上传构建为非官方包，按名称、平台和 SHA256 固定选择；留空使用官方包。";
    if (automatic && compatibility) hint.textContent = `${automatic}${compatibility}`;
  }
}

function renderManagedVersionCompatibility(prefix) {
  const elements = managedLegElements(prefix);
  const output = $(`#${prefix}MinerVersionCompatibility`);
  if (!output) return;
  const packageId = elements.type.value === "seine" ? $(`#${prefix}NativePackageId`)?.value : "";
  if (packageId) {
    const minerPackage = (state.minerPackages || []).find((item) => item.id === packageId);
    const available = seineFlightPackages(elements.version.value).some((item) => item.id === packageId);
    output.classList.toggle("warning", !available);
    const details = minerPackage
      ? `<span>${escapeHtml(seineFlightPackageLabel(minerPackage))}</span><span>SHA256 <code>${escapeHtml(minerPackage.sha256 || "未知")}</code></span>`
      : `<span>包 ID：${escapeHtml(packageId)}</span>`;
    output.innerHTML = `<strong>${available ? "固定上传构建（非官方）" : "绑定上传构建不可用"}</strong>${details}<span>${available ? "仅使用所选软件包；不自动替换为同版本官方包。" : "请明确选择有效上传包，或选择自动官方包以清除绑定；不会静默退回官方包。"}</span>`;
    return;
  }
  const entry = managedVersionEntry(elements.type.value, elements.version.value);
  if (!entry) {
    output.classList.add("warning");
    output.innerHTML = `<strong>版本 ${escapeHtml(elements.version.value || "未知")} 当前在控制端版本目录中不可用</strong><span>编辑时可原样保留；重新下载或下发前请改选当前可用版本。</span>`;
    return;
  }
  const versionAvailable = managedVersionIsAvailable(elements.type.value, elements.version.value);
  const genericTestGroup = ["windows", "linux", "linux-nvidia", "linux-amd"]
    .some((platform) => String(managedVersionAsset(entry, platform)?.package_id || "").startsWith("test-"));
  const seineUploads = elements.type.value === "seine"
    ? Object.fromEntries(seineFlightPackages(elements.version.value).map((item) => [item.managed_os_family, item]))
    : {};
  const testGroup = genericTestGroup || Boolean(Object.keys(seineUploads).length);
  output.classList.toggle("warning", elements.type.value === "seine" && !versionAvailable);
  const usesWsl = elements.executionMode?.value === "wsl";
  const platforms = usesWsl
    ? [[elements.type.value === "pearlminer" ? "linux-nvidia" : "linux", "WSL · Linux NVIDIA"]]
    : (elements.type.value === "pearlminer"
      ? [["windows", "Windows"], ["linux-nvidia", "Linux NVIDIA"], ["linux-amd", "Linux AMD"]]
      : [["windows", "Windows"], ["linux", "Linux"]]);
  const platformMarkup = platforms.map(([platform, label]) => {
    const asset = platform === "linux-nvidia"
      ? (managedVersionAsset(entry, "linux-nvidia") || managedVersionAsset(entry, "linux"))
      : managedVersionAsset(entry, platform);
    const filename = managedAssetFilename(asset);
    const uploadedPackage = seineUploads[platform] || null;
    const genericUploadedBuild = elements.type.value !== "seine"
      && String(asset?.package_id || "").startsWith("test-");
    const uploadedBuild = Boolean(uploadedPackage) || genericUploadedBuild;
    const available = Boolean(uploadedPackage) || genericUploadedBuild;
    const description = uploadedBuild
      ? uploadedPackage
        ? `${uploadedPackage.name || uploadedPackage.id} · 上传构建（非官方）`
        : `${filename || "上传构建"} · 上传构建（非官方）`
      : elements.type.value === "seine" && Object.keys(seineUploads).length
        ? "缺少此平台上传构建；不会自动改用官方包"
        : available ? (filename || "可用，文件名由控制端管理") : "此版本无对应安装包";
    return `<span class="platform-asset ${available ? "available" : "unavailable"}"><strong>${label}</strong><code>${escapeHtml(description)}</code></span>`;
  }).join("");
  output.innerHTML = (testGroup
    ? "<strong>同一上传构建版本按目标设备系统分发软件包；缺少对应平台时明确失败，不会自动改用官方包。WSL 使用 Linux 包。</strong>"
    : elements.type.value === "seine" && !versionAvailable ? "<strong>当前平台没有可用软件包；请选择当前可用版本或上传对应平台构建。</strong>" : "") + platformMarkup;
}

function pearlMinerPoolUrlSupported(value) {
  return /^(?:pearl|stratum\+(?:tcp|ssl)):\/\//i.test(String(value || "").trim());
}

function syncManagedLeg(prefix, { preserveAlgorithm = false, selectedVersion = "", resetVersion = false } = {}) {
  const elements = managedLegElements(prefix);
  const minerType = elements.type.value;
  const isXmrig = minerType === "xmrig";
  const isPearl = minerType === "pearlminer";
  const hasAdapter = Boolean(minerAdapterDefinition(minerType));
  $(`#${prefix}ExtraArgsField`)?.classList.toggle("hidden", !hasAdapter);
  if (elements.extraArgs) elements.extraArgs.disabled = !hasAdapter;
  const adapterOptions = minerAdapterOptionNames(minerType);
  const adapterAlgorithms = minerAdapterAlgorithms(minerType);
  if (prefix === "flightSecondary" && hasAdapter && usesIntegratedSrbDual()) {
    selectedVersion = $("#flightMinerVersion").value || selectedVersion;
    resetVersion = false;
  }
  populateManagedVersionSelect(prefix, selectedVersion, resetVersion);
  populateSeineFlightPackageSelect(prefix);
  const requiredCoin = isXmrig ? "XMR" : (isPearl ? "PEARL" : "");
  const requiredCoins = requiredCoin ? [requiredCoin] : [];
  if (requiredCoins.length && !requiredCoins.includes(elements.wallet.selectedOptions[0]?.dataset.coin?.toUpperCase())) {
    const matchingOption = [...elements.wallet.options].find((option) => requiredCoins.includes(option.dataset.coin?.toUpperCase()));
    if (matchingOption) elements.wallet.value = matchingOption.value;
  }
  syncFlightCoin(`#${prefix}Wallet`, `#${prefix}Coin`);
  const coin = elements.coin.value.toUpperCase();
  populateManagedAlgorithmSelect(prefix, preserveAlgorithm ? elements.algorithm.value : "");
  const walletError = isXmrig && coin !== "XMR"
    ? "XMRig 简单模式需要先添加并选择 XMR 钱包"
    : (isPearl && coin !== "PEARL" ? "Pearl Miner 需要先添加并选择 PEARL 钱包" : "");
  elements.wallet.setCustomValidity(walletError);
  elements.algorithm.disabled = adapterAlgorithms.length <= 1;
  elements.devices.disabled = !minerAdapterHasCapability(minerType, "device_selection");
  const deviceHint = $(`#${prefix}DeviceIndicesHint`);
  if (deviceHint) deviceHint.textContent = prefix === "flight"
      ? "使用所选矿工报告的 GPU 索引；留空会自动过滤核显并选择当前算法支持的独显。"
      : "留空会自动选择支持的独显；内置双挖须与主挖索引一致，两个独立 GPU 矿工须明确填写且互不重叠。";
  if (isXmrig) {
    elements.algorithm.value = "";
    elements.devices.value = "";
  } else if (isPearl) {
    elements.algorithm.value = "pearlhash";
    elements.devices.value = "";
  } else if (!preserveAlgorithm || !elements.algorithm.value) {
    elements.algorithm.value = minerAdapterDefaultAlgorithm(minerType, coin) || "";
  }
  syncManagedExecutionMode(prefix);
  syncCpuRandomxFields(prefix);
  syncManagedExtraArgsHint(prefix);

  if (prefix === "flight") {
    const definition = managedMinerDefinition(minerType);
    const latestPreview = isPearl
      ? managedMinerVersions(definition).find(managedVersionIsPreview)
      : null;
    $("#flightMinerVersionHint").textContent = definition
      ? (isPearl
        ? `${definition.name} 默认稳定版 ${definition.latest_version || definition.version || "未知"}${latestPreview ? `；最新测试版 ${latestPreview.version}` : ""}；测试版需显式选择。`
        : `${definition.name} 最新版 ${definition.latest_version || definition.version || "未知"}；共 ${managedMinerVersions(definition).length} 个可用版本，可任意选择。`)
      : "控制端暂未同步到可用版本。";
    $("#flightPearlGpuFields").classList.toggle("hidden", !isPearl);
    $("#flightPoolPasswordField").classList.toggle("hidden", isPearl || minerType === "seine");
    if (minerType === "seine") $("#flightPoolPassword").value = "";
    $("#flightPoolLabelText").textContent = isPearl
      ? "Pearl Proxy / Stratum TCP/SSL 地址"
      : "Stratum 矿池地址";
    const poolSchemes = minerAdapterDefinition(minerType)?.pool_schemes;
    const tcpOnlyPool = Array.isArray(poolSchemes) && poolSchemes.length > 0 && poolSchemes.every((scheme) => scheme === "stratum+tcp");
    $("#flightPool").placeholder = tcpOnlyPool ? "stratum+tcp://pool.example.com:3333" : "stratum+ssl://pool.example.com:443";
    $("#flightPoolHint").innerHTML = isPearl
      ? '<code>pearl://</code> 使用 Pearl Proxy；也可直接填写 <code>stratum+tcp://host:port</code> 或 <code>stratum+ssl://host:port</code>。官方 Proxy：<code>pearl://global.pearlfortune.org:443</code>、<code>pearl://jp.pearlfortune.org:443</code>；地址必须包含协议和端口。'
      : tcpOnlyPool
        ? '当前矿工仅支持 Stratum TCP；请填写 <code>stratum+tcp://host:port</code>，地址必须包含协议和端口。'
        : '必须包含协议和端口；使用 <code>stratum+ssl://</code> 或 <code>stratum+tls://</code> 时自动启用 TLS。';
    if (isPearl && !$("#flightPool").value.trim()) {
      $("#flightPool").value = "pearl://global.pearlfortune.org:443";
    } else if (!isPearl && !preserveAlgorithm && $("#flightPool").value.trim() === "pearl://global.pearlfortune.org:443") {
      $("#flightPool").value = "";
    }
    if (elements.gpuBackend) {
      const isAmd = isPearl && elements.gpuBackend.value === "amd";
      $("#flightCudaVariantField").classList.toggle("hidden", isAmd);
      elements.cudaVariant.disabled = !isPearl || isAmd;
      if (isAmd) elements.cudaVariant.value = "auto";
      elements.gpuBackend.disabled = !isPearl || elements.executionMode.value === "wsl";
    }
  } else {
    $("#flightSecondaryPearlGpuFields").classList.toggle("hidden", !isPearl);
    $("#flightSecondaryPoolPasswordField").classList.toggle("hidden", isPearl || minerType === "seine");
    if (minerType === "seine") $("#flightSecondaryPoolPassword").value = "";
    $("#flightSecondaryPoolLabelText").textContent = isPearl
      ? "Pearl Proxy / Stratum TCP/SSL 地址"
      : "Stratum 矿池地址";
    const poolSchemes = minerAdapterDefinition(minerType)?.pool_schemes;
    const tcpOnlyPool = Array.isArray(poolSchemes) && poolSchemes.length > 0 && poolSchemes.every((scheme) => scheme === "stratum+tcp");
    $("#flightSecondaryPool").placeholder = isPearl ? "stratum+ssl://pool.example.com:443" : tcpOnlyPool ? "stratum+tcp://secondary-pool.example.com:3333" : "stratum+ssl://secondary-pool.example.com:443";
    $("#flightSecondaryPoolHint").innerHTML = isPearl
      ? '<code>pearl://</code> 使用 Pearl Proxy；也可直接填写 <code>stratum+tcp://host:port</code> 或 <code>stratum+ssl://host:port</code>。官方 Proxy：<code>pearl://global.pearlfortune.org:443</code>、<code>pearl://jp.pearlfortune.org:443</code>；地址必须包含协议和端口。'
      : tcpOnlyPool
        ? '当前矿工仅支持 Stratum TCP；请填写 <code>stratum+tcp://host:port</code>，地址必须包含协议和端口；独立矿工可使用与主矿工不同的矿池。'
        : '必须包含协议和端口；独立矿工可使用与主矿工不同的矿池。';
    if (isPearl && !$("#flightSecondaryPool").value.trim()) {
      $("#flightSecondaryPool").value = "pearl://global.pearlfortune.org:443";
    } else if (!isPearl && !preserveAlgorithm && $("#flightSecondaryPool").value.trim() === "pearl://global.pearlfortune.org:443") {
      $("#flightSecondaryPool").value = "";
    }
    const isAmd = isPearl && elements.gpuBackend.value === "amd";
    $("#flightSecondaryCudaVariantField").classList.toggle("hidden", isAmd);
    elements.cudaVariant.disabled = !isPearl || isAmd;
    if (isAmd) elements.cudaVariant.value = "auto";
    elements.gpuBackend.disabled = !isPearl || elements.executionMode.value === "wsl";
  }
  renderManagedVersionCompatibility(prefix);
  syncFlightGpuOcVisibility();
  syncManagedProtocolUI(prefix);
}

function setManagedExtra(prefix, extra = {}) {
  const elements = managedLegElements(prefix);
  if (elements.extraArgs) elements.extraArgs.value = typeof extra.extra_args === "string" ? extra.extra_args : "";
  // Controls are generated per adapter.  Remove controls from the previously
  // rendered adapter before seeding values, otherwise their live DOM values
  // can overwrite the flight sheet that is being opened next.
  resetAdapterSchemaOptionValues(prefix, extra);
  elements.cpuThreads.value = Number.isInteger(extra.cpu_threads) ? String(extra.cpu_threads) : "";
  elements.cpuHint.value = Number.isInteger(extra.cpu_max_threads_hint) ? String(extra.cpu_max_threads_hint) : "100";
  elements.hugePages.checked = extra.huge_pages !== false;
  if (elements.gpuBackend) elements.gpuBackend.value = ["auto", "nvidia", "amd"].includes(extra.gpu_backend) ? extra.gpu_backend : "auto";
  if (elements.cudaVariant) elements.cudaVariant.value = ["auto", "cuda12", "cuda13"].includes(extra.cuda_variant) ? extra.cuda_variant : "auto";
}

function addFlightGpuOcRow(values = {}) {
  const row = document.createElement("div");
  row.className = "gpu-setting-row flight-gpu-oc-row";
  const value = (field, fallback = "") => escapeHtml(values[field] ?? fallback);
  const legacyIndex = Number.isInteger(values.index) ? String(values.index) : "";
  row.innerHTML = `<label>显卡型号匹配<input type="text" maxlength="96" ${legacyIndex ? "" : "required"} data-flight-gpu-field="model_match" value="${value("model_match")}" placeholder="例如 5080"><small data-flight-gpu-index-hint>${legacyIndex ? `旧配置：SRBMiner GPU ${escapeHtml(legacyIndex)}；填写型号即可升级` : "按实际显卡名称自动匹配"}</small></label>
    <label>Core Clock Offset (MHz)<input type="number" min="-2000" max="2000" step="1" data-flight-gpu-field="core_clock_offset_mhz" value="${value("core_clock_offset_mhz")}" placeholder="可选"><small>SRBMiner --gpu-coffset</small></label>
    <label>Lock Core Clock (MHz)<input type="number" min="100" max="5000" step="1" data-flight-gpu-field="core_clock_lock_mhz" value="${value("core_clock_lock_mhz")}" placeholder="可选"><small>SRBMiner 固定核心时钟</small></label>
    <label>Memory Clock Offset (MHz)<input type="number" min="-5000" max="5000" step="1" data-flight-gpu-field="memory_clock_offset_mhz" value="${value("memory_clock_offset_mhz")}" placeholder="可选"><small>SRBMiner --gpu-moffset</small></label>
    <label>Lock Memory Clock (MHz)<input type="number" min="100" max="20000" step="1" data-flight-gpu-field="memory_clock_lock_mhz" value="${value("memory_clock_lock_mhz")}" placeholder="可选"><small>固定显存时钟</small></label>
    <label>Power Limit (W)<input type="number" min="0" step="any" data-flight-gpu-field="power_limit_w" value="${value("power_limit_w")}" placeholder="可选"><small>最大功耗瓦数</small></label>
    <button class="icon-button remove-setting" type="button" data-remove-flight-gpu-row aria-label="删除此 GPU OC 设置">×</button>`;
  if (legacyIndex) row.dataset.legacyGpuIndex = legacyIndex;
  $("#flightGpuOcRows").append(row);
  $("[data-remove-flight-gpu-row]", row).addEventListener("click", () => row.remove());
  $("[data-flight-gpu-field=model_match]", row).addEventListener("input", (event) => {
    if (!event.target.value.trim()) return;
    delete row.dataset.legacyGpuIndex;
    event.target.required = true;
    $("[data-flight-gpu-index-hint]", row).textContent = "按实际显卡名称自动匹配";
  });
  updateFlightGpuOcIndexHints();
}

function collectFlightGpuOc() {
  const rows = $$(".flight-gpu-oc-row", $("#flightGpuOcRows"));
  if (!rows.length) return null;
  if (
    usesIntegratedSrbDual()
    && managedLegResource("flight") === "gpu"
    && managedLegResource("flightSecondary") === "gpu"
  ) {
    const primaryOrder = $("#flightDeviceIndices").value.split(",").map((item) => item.trim()).filter(Boolean);
    const secondaryOrder = $("#flightSecondaryDeviceIndices").value.split(",").map((item) => item.trim()).filter(Boolean);
    if ((primaryOrder.length || secondaryOrder.length) && primaryOrder.join(",") !== secondaryOrder.join(",")) {
      throw new Error("GPU 双挖的主挖与副挖必须填写完全相同且顺序一致的 GPU 索引。");
    }
  }
  const settings = rows.map((row, position) => {
    const modelMatch = $("[data-flight-gpu-field=model_match]", row).value.trim();
    let setting;
    if (modelMatch) {
      if (modelMatch.length > 96 || /[\u0000-\u001f\u007f]/.test(modelMatch)) {
        throw new Error(`第 ${position + 1} 行显卡型号匹配格式无效。`);
      }
      const matcherKey = modelMatch.toLowerCase().replace(/[^a-z0-9]+/g, "");
      if (matcherKey.length < 2) throw new Error(`第 ${position + 1} 行显卡型号至少包含两个字母或数字。`);
      setting = { model_match: modelMatch };
    } else if (row.dataset.legacyGpuIndex !== undefined) {
      const index = Number(row.dataset.legacyGpuIndex);
      if (!Number.isInteger(index) || index < 0 || index > 31) throw new Error(`第 ${position + 1} 行旧 GPU Index 无效。`);
      setting = { index };
    } else {
      throw new Error(`第 ${position + 1} 行请填写显卡型号，例如 5080、5070 或 5060 Ti。`);
    }
    const fields = ["core_clock_offset_mhz", "core_clock_lock_mhz", "memory_clock_offset_mhz", "memory_clock_lock_mhz", "power_limit_w"];
    fields.forEach((field) => {
      const input = $(`[data-flight-gpu-field=${field}]`, row);
      const raw = input.value.trim();
      if (input.validity?.badInput) throw new Error(`第 ${position + 1} 行 ${field} 超出允许范围。`);
      if (raw !== "") {
        const number = Number(raw);
        if (!gpuOcNumberIsValid(number, field) || !input.checkValidity()) throw new Error(`第 ${position + 1} 行 ${field} 超出允许范围。`);
        setting[field] = number;
      }
    });
    if (Object.keys(setting).length === 1) throw new Error(`第 ${position + 1} 行 GPU OC 至少填写一项调节值，或删除该行。`);
    return setting;
  });
  const locatorModes = new Set(settings.map((setting) => (setting.model_match ? "model" : "index")));
  if (locatorModes.size !== 1) {
    throw new Error("旧索引 OC 与型号匹配 OC 不能混用；请为旧行填写显卡型号后再保存。");
  }
  const targetKeys = settings.map((setting) => (
    setting.model_match
      ? `model:${setting.model_match.toLowerCase().replace(/[^a-z0-9]+/g, "")}`
      : `index:${setting.index}`
  ));
  if (new Set(targetKeys).size !== settings.length) {
    throw new Error("GPU OC 不能重复填写同一个显卡型号匹配规则。");
  }
  const controlSignatures = new Set(settings.map((setting) => Object.keys(setting).filter((field) => !["index", "model_match"].includes(field)).sort().join(",")));
  if (controlSignatures.size !== 1) {
    throw new Error("所有 GPU OC 行必须填写相同的控制列；数值可以逐卡不同。");
  }
  return { vendor: "nvidia", settings };
}

function setFlightAlgorithmValue(selector, algorithm) {
  const select = $(selector);
  const value = String(algorithm || "").toLowerCase();
  if (value && ![...select.options].some((option) => option.value === value)) {
    const option = new Option(`${value} · 历史算法`, value);
    option.dataset.historyAlgorithm = "true";
    select.add(option);
  }
  select.value = value;
}

function flightSheetMigrationReason(sheet) {
  if (!sheet) return "";
  if (sheet.miner_type === "generic") return "旧版 generic / profile 飞行表";
  const unsupported = [sheet.miner_type, sheet.secondary?.miner_type].find(
    (minerType) => minerType && minerType !== "generic" && !minerAdapterDefinition(minerType),
  );
  return unsupported ? `${RETIRED_MINER_LABELS[unsupported] || unsupported} 适配器已退休或当前不可用` : "";
}

function setFlightFormHistoricalReadOnly(readOnly) {
  state.flightHistoricalReadOnly = Boolean(readOnly);
  const form = $("#flightForm");
  $$('input, select, textarea, button', form).forEach((element) => {
    if (element.matches('[data-close-dialog], #migrateFlightButton')) return;
    element.disabled = Boolean(readOnly);
  });
  $("#createFlightButton").disabled = Boolean(readOnly);
}

function beginFlightMigrationCopy() {
  const source = state.flightSheets.find((item) => item.id === state.flightMigrationSourceId);
  if (!source || !state.flightHistoricalReadOnly) return;
  state.editingFlightSheetId = null;
  setFlightFormHistoricalReadOnly(false);
  $("#flightDialogTitle").textContent = "迁移飞行表副本";
  $("#flightName").value = `${String(source.name || "历史飞行表").replace(/（迁移副本）$/, "")}（迁移副本）`;
  $("#migrateFlightButton").classList.add("hidden");
  $("#createFlightButton").textContent = "创建迁移副本";
  $("#flightLegacyNoticeText").textContent = "正在创建一个全新的飞行表副本；原历史记录不会修改。请明确选择当前支持的矿工，再逐项核对版本、钱包、矿池、算法和软件参数。";
  $("#flightError").textContent = "历史矿工身份已保留；请选择当前支持的矿工后再创建副本。";
  setSecondaryEnabled($("#flightSecondaryEnabled").checked);
  syncManagedLeg("flight", { preserveAlgorithm: true, selectedVersion: $("#flightMinerVersion").value });
  if ($("#flightSecondaryEnabled").checked) {
    syncSecondaryMode({ preserveAlgorithm: true, selectedVersion: $("#flightSecondaryMinerVersion").value });
  } else {
    syncManagedLeg("flightSecondary", { preserveAlgorithm: true, selectedVersion: $("#flightSecondaryMinerVersion").value });
  }
}

function openFlightDialog(sheetId = null) {
  if (!state.wallets.length) {
    toast("请先添加一个公开钱包地址", "error");
    showPage("wallets");
    return;
  }
  closeFlightMinerConfig({ restoreFocus: false });
  resetResourceEditor($("#createFlightButton"));
  $("#flightForm").reset();
  $("#flightError").textContent = "";
  const sheet = sheetId ? state.flightSheets.find((item) => item.id === sheetId) : null;
  state.editingFlightSheetId = sheet?.id || null;
  state.flightMigrationSourceId = null;
  state.flightHistoricalReadOnly = false;
  setFlightFormHistoricalReadOnly(false);
  $("#flightLegacyNotice").classList.add("hidden");
  $("#migrateFlightButton").classList.add("hidden");
  $("#flightDialogTitle").textContent = sheet ? "编辑飞行表" : "新建飞行表";
  $("#createFlightButton").textContent = sheet ? "保存修改" : "保存飞行表";
  $("#flightWorker").value = "%WORKER_NAME%";
  $("#flightPoolPassword").value = "x";
  $("#flightSecondaryPoolPassword").value = "x";
  for (const prefix of ["flight", "flightSecondary"]) delete $(`#${prefix}PoolPassword`).dataset.dirty;
  $("#flightWallet").innerHTML = walletOptions();
  $("#flightSecondaryWallet").innerHTML = walletOptions();
  populateManagedMinerTypeSelect("flight", "xmrig");
  $("#flightExecutionMode").value = "native";
  $("#flightSecondaryMode").value = "independent";
  populateManagedMinerTypeSelect("flightSecondary", "pearlminer");
  $("#flightSecondaryExecutionMode").value = "native";
  $("#flightSecondaryMinerType").disabled = false;
  $("#flightSecondaryMinerVersion").disabled = false;
  $$("option[data-history-algorithm]", $("#flightForm")).forEach((option) => option.remove());
  $("#flightGpuOcRows").innerHTML = "";
  setManagedExtra("flight");
  setManagedExtra("flightSecondary");
  setSecondaryEnabled(false);
  ["#flightAdvanced", "#flightSecondaryAdvanced"].forEach((selector) => {
    const details = $(selector);
    if (details) details.open = false;
  });

  if (sheet) {
    const primaryType = sheet.miner_type || "generic";
    populateManagedMinerTypeSelect("flight", primaryType);
    $("#flightName").value = sheet.name || "";
    $("#flightMinerType").value = primaryType;
    $("#flightExecutionMode").value = sheet.execution_mode === "wsl" ? "wsl" : "native";
    $("#flightWallet").value = sheet.wallet_id;
    $("#flightPool").value = sheet.pool_url || "";
    setFlightAlgorithmValue("#flightAlgorithm", sheet.algorithm);
    $("#flightPoolPassword").value = sheet.pool_password ?? "x";
    $("#flightPoolPassword").dataset.dirty = "true";
    $("#flightDeviceIndices").value = (sheet.device_indices || []).join(",");
    $("#flightWorker").value = normalizeWorkerNameTemplate(sheet.worker_name_template || "%WORKER_NAME%");
    setManagedExtra("flight", sheet.extra_config || {});
    const gpuOc = sheet.gpu_oc && typeof sheet.gpu_oc === "object" ? sheet.gpu_oc : null;
    if (gpuOc?.vendor === "nvidia" && Array.isArray(gpuOc.settings)) {
      gpuOc.settings.forEach((setting) => addFlightGpuOcRow(setting));
      if (gpuOc.settings.length) $("#flightAdvanced").open = true;
    }
    if (sheet.secondary) {
      const secondaryType = sheet.secondary.miner_type || "srbminer";
      populateManagedMinerTypeSelect("flightSecondary", secondaryType);
      const secondaryMode = ["integrated", "independent"].includes(sheet.secondary_mode)
        ? sheet.secondary_mode
        : "independent";
      $("#flightSecondaryMode").value = secondaryMode;
      $("#flightSecondaryMinerType").value = secondaryType;
      $("#flightSecondaryExecutionMode").value = (sheet.secondary.execution_mode || sheet.secondary_execution_mode) === "wsl" ? "wsl" : "native";
      $("#flightSecondaryWallet").value = sheet.secondary.wallet_id;
      $("#flightSecondaryPool").value = sheet.secondary.pool_url || "";
      setFlightAlgorithmValue("#flightSecondaryAlgorithm", sheet.secondary.algorithm);
      $("#flightSecondaryPoolPassword").value = sheet.secondary.pool_password ?? "x";
      $("#flightSecondaryPoolPassword").dataset.dirty = "true";
      $("#flightSecondaryDeviceIndices").value = (sheet.secondary.device_indices || []).join(",");
      setManagedExtra("flightSecondary", sheet.secondary.extra_config || {});
      setSecondaryEnabled(true);
    }
  }
  const primarySelection = sheet?.miner_version;
  syncManagedLeg("flight", { preserveAlgorithm: Boolean(sheet), selectedVersion: primarySelection || "", resetVersion: !primarySelection });
  if (sheet?.secondary) {
    syncSecondaryMode({
      preserveAlgorithm: true,
      selectedVersion: sheet.secondary.miner_version || "",
      resetVersion: !sheet.secondary.miner_version,
    });
  } else {
    syncManagedLeg("flightSecondary", { resetVersion: true });
  }
  for (const [prefix, leg] of [["flight", sheet], ["flightSecondary", sheet?.secondary]]) {
    populateSeineFlightPackageSelect(prefix, leg?.miner_type === "seine" ? leg.miner_package_id || "" : "");
    if (leg?.miner_type === "seine") renderManagedVersionCompatibility(prefix);
  }
  const migrationReason = flightSheetMigrationReason(sheet);
  if (sheet && migrationReason) {
    state.flightMigrationSourceId = sheet.id;
    $("#flightDialogTitle").textContent = "查看历史飞行表";
    $("#flightLegacyNotice").classList.remove("hidden");
    $("#flightLegacyNoticeText").textContent = `${migrationReason}。原矿工身份与历史记录已保留，当前只读。创建副本时必须明确改选当前支持的矿工，原记录不会被覆盖。`;
    $("#migrateFlightButton").classList.remove("hidden");
    $("#createFlightButton").textContent = "历史记录只读";
    $("#flightError").textContent = "如需继续使用，请点击“迁移为副本”，明确选择当前矿工并核对配置。";
    setFlightFormHistoricalReadOnly(true);
  }
  $("#flightSavedPool").value = "";
  $("#flightSecondarySavedPool").value = "";
  populateSavedPoolSelectors();
  $("#flightDialog").showModal();
}

function parseDeviceIndices(selector, label) {
  const raw = $(selector).value.trim();
  if (!raw) return [];
  const parts = raw.split(",").map((value) => value.trim());
  if (parts.some((value) => !/^\d+$/.test(value))) throw new Error(`${label}只能填写 0–31 的 GPU 索引，并用逗号分隔。`);
  const values = parts.map(Number);
  if (values.some((value) => !Number.isInteger(value) || value < 0 || value > 31)) throw new Error(`${label}只能填写 0–31 的 GPU 索引，并用逗号分隔。`);
  if (new Set(values).size !== values.length) throw new Error(`${label}不能包含重复的 GPU 索引。`);
  return values;
}

function sameGpuIndices(left, right) {
  if (left.length !== right.length) return false;
  return left.every((index, position) => index === right[position]);
}

function validateSrbDualFlight(primary, secondary) {
  if (primary.miner_version !== secondary.miner_version) {
    throw new Error("SRBMiner 双挖的主挖和副挖必须使用同一个矿工版本。");
  }
  const primaryAlgorithm = String(primary.algorithm || "").toLowerCase();
  const secondaryAlgorithm = String(secondary.algorithm || "").toLowerCase();
  if (primaryAlgorithm === "randomx" && secondaryAlgorithm === "randomx") {
    throw new Error("SRBMiner 双挖不能同时选择两个 RandomX CPU 算法。");
  }
  const pr = minerAdapterAlgorithmResource("srbminer", primaryAlgorithm, primary.extra_config?.resource_kind);
  const sr = minerAdapterAlgorithmResource("srbminer", secondaryAlgorithm, secondary.extra_config?.resource_kind);
  const supported = srbDualPairSupported(primaryAlgorithm, secondaryAlgorithm, pr, sr);
  if (!supported) {
    throw new Error(`SRBMiner 不支持该双挖组合：${primaryAlgorithm} + ${secondaryAlgorithm}。`);
  }
  const threadsRule = minerAdapterDefinition("srbminer")?.periodic_cpu_threads_rule;
  if (threadsRule?.requires_matching_automatic_status
      && threadsRule.primary_algorithms?.includes(primaryAlgorithm)
      && threadsRule.resources?.length === 2
      && [pr, sr].every((resource, index) => resource === threadsRule.resources[index])
      && (primary.extra_config?.[threadsRule.option] == null) !== (secondary.extra_config?.[threadsRule.option] == null)) {
    throw new Error(threadsRule.hint || threadsRule.message);
  }
  if (pr === "gpu" && sr === "gpu"
      && !sameGpuIndices(primary.device_indices, secondary.device_indices)) {
    throw new Error("GPU + GPU 双挖的主挖和副挖必须使用相同顺序的 GPU 索引。");
  }
}

function validateIntegratedDualFlight(primary, secondary) {
  const adapter = minerAdapterDefinition(primary.miner_type);
  const name = adapter?.display_name || primary.miner_type;
  if (!minerAdapterHasCapability(primary.miner_type, "integrated_dual") || secondary.miner_type !== primary.miner_type) {
    throw new Error(`${name} 内置双挖要求主挖与第二矿工使用同一个支持内置双挖的矿工。`);
  }
  if (primary.miner_version !== secondary.miner_version) throw new Error(`${name} 内置双挖必须使用同一个矿工版本。`);
  if (primary.miner_type === "srbminer") {
    validateSrbDualFlight(primary, secondary);
    return;
  }
  const primaryAlgorithm = String(primary.algorithm || "").toLowerCase();
  const secondaryAlgorithm = String(secondary.algorithm || "").toLowerCase();
  if (!(adapter?.dual_algorithm_pairs || []).some((pair) => pair[0] === primaryAlgorithm && pair[1] === secondaryAlgorithm)) {
    throw new Error(`${name} 不支持所选算法的内置双挖顺序：${primaryAlgorithm} + ${secondaryAlgorithm}。`);
  }
}

function editingFlightVersionIsUnchanged(prefix, minerType, minerVersion) {
  if (!state.editingFlightSheetId) return false;
  const sheet = state.flightSheets.find((item) => item.id === state.editingFlightSheetId);
  if (!sheet) return false;
  const original = prefix === "flight" ? sheet : sheet.secondary;
  if (!original) return false;
  const originalType = prefix === "flight" ? sheet.miner_type : (original.miner_type || "srbminer");
  return originalType === minerType && String(original.miner_version || "") === String(minerVersion);
}

function buildManagedLeg(prefix, label) {
  const elements = managedLegElements(prefix);
  const minerType = elements.type.value;
  if (!minerAdapterDefinition(minerType)) throw new Error(`${label}矿工 ${RETIRED_MINER_LABELS[minerType] || minerType || "未知"} 已退休或不受支持；请明确选择当前支持的矿工。`);
  const adapterOptions = minerAdapterOptionNames(minerType);
  const usesCpuRandomx = managedLegUsesCpuRandomX(prefix);
  const minerVersion = elements.version.value;
  const packageId = minerType === "seine" ? String($(`#${prefix}NativePackageId`)?.value || "") : "";
  const pinnedPackage = packageId ? seineFlightPackages(minerVersion).find((item) => item.id === packageId) : null;
  const poolUrl = $(`#${prefix}Pool`).value.trim();
  const coin = elements.wallet.selectedOptions[0]?.dataset.coin?.toUpperCase() || "";
  const executionMode = elements.executionMode?.value === "wsl" ? "wsl" : "native";
  if (!minerVersion) throw new Error(`${label}没有可用的矿工版本。`);
  if (packageId && !pinnedPackage) throw new Error(`${label}绑定的上传构建不可用或与当前类型/版本不匹配；请明确选择矿工包来源，不会自动改用官方包。`);
  if (!pinnedPackage && !managedVersionIsAvailable(minerType, minerVersion) && !editingFlightVersionIsUnchanged(prefix, minerType, minerVersion)) {
    throw new Error(`${label}矿工版本 ${minerVersion} 当前不可下载或未在控制端缓存；请选择当前可用版本。`);
  }
  if (minerType === "xmrig" && coin !== "XMR") throw new Error(`${label}使用 XMRig 时必须选择 XMR 钱包。`);
  if (minerType === "pearlminer" && coin !== "PEARL") throw new Error(`${label}使用 Pearl Miner 时必须选择 PEARL 钱包。`);
  let algorithm = minerType === "xmrig"
    ? null
    : (elements.algorithm.value || minerAdapterDefaultAlgorithm(minerType, coin) || null);
  if (minerType !== "xmrig" && !algorithm) throw new Error(`${label}币种没有内置推荐算法，请在高级设置中选择算法。`);
  if (executionMode === "wsl" && !managedLegSupportsWsl(prefix)) {
    throw new Error(`${label}只有 Windows GPU 矿工可以使用 WSL；XMRig、CPU 任务和内置双挖必须使用本机原生模式。`);
  }
  if (executionMode === "wsl" && managedVersionEntry(minerType, minerVersion) && !managedVersionWslAsset(minerType, minerVersion)) {
    throw new Error(`${label}矿工版本 ${minerVersion} 没有可供 WSL 使用的 Linux NVIDIA 包。`);
  }
  if (executionMode === "wsl" && minerType === "pearlminer" && elements.gpuBackend.value !== "nvidia") {
    throw new Error(`${label}的 WSL 模式当前只支持 Linux NVIDIA 矿工包。`);
  }
  const contextReason = managedLegProtocolReason(prefix);
  if (contextReason) throw new Error(`${label}${contextReason}`);
  const extraConfig = {};
  if (adapterOptions.has("gpu_backend")) extraConfig.gpu_backend = elements.gpuBackend.value;
  if (adapterOptions.has("cuda_variant")) {
    extraConfig.cuda_variant = adapterOptions.has("gpu_backend") && elements.gpuBackend.value === "amd" ? "auto" : elements.cudaVariant.value;
  }
  if (usesCpuRandomx && adapterOptions.has("huge_pages")) extraConfig.huge_pages = elements.hugePages.checked;
  if (usesCpuRandomx && adapterOptions.has("randomx_1gb_pages")) extraConfig.randomx_1gb_pages = false;
  if (adapterOptions.has("keepalive")) extraConfig.keepalive = true;
  const threads = usesCpuRandomx && adapterOptions.has("cpu_threads") ? elements.cpuThreads.value.trim() : "";
  if (threads) {
    const threadCount = Number(threads);
    const threadSpec = minerAdapterOptionSpec(minerType, "cpu_threads");
    const schemaMaximum = Number.isInteger(threadSpec?.maximum) && threadSpec.maximum >= 1 ? threadSpec.maximum : null;
    const maximum = minerType === "xmrig" ? Math.min(schemaMaximum ?? 256, 256) : schemaMaximum;
    if (!Number.isSafeInteger(threadCount) || threadCount < 1 || (maximum !== null && threadCount > maximum)) throw new Error(`${label} CPU 线程数必须是${maximum === null ? "正整数" : ` 1–${maximum} 的整数`}。`);
    extraConfig.cpu_threads = threadCount;
  }
  if (usesCpuRandomx && adapterOptions.has("cpu_max_threads_hint")) {
    extraConfig.cpu_max_threads_hint = Number(elements.cpuHint.value || 100);
  }
  if (usesCpuRandomx && adapterOptions.has("pause_on_battery")) {
    extraConfig.pause_on_battery = false;
  }
  if (adapterOptions.has("extended_log")) {
    extraConfig.extended_log = false;
  }
  Object.assign(extraConfig, collectAdapterSchemaOptions(prefix));
  if (elements.extraArgs) extraConfig.extra_args = elements.extraArgs.value;
  if (usesCpuRandomx && extraConfig.cpu_threads != null && extraConfig.cpu_threads_reduce != null) {
    throw new Error(`${label}不能同时设置“CPU 挖矿线程 / 核心数”和“减少 CPU 线程数”。`);
  }
  return {
    miner_type: minerType,
    miner_version: minerVersion,
    execution_mode: executionMode,
    coin,
    wallet_id: elements.wallet.value,
    pool_url: poolUrl,
    miner_profile: "default",
    miner_package_id: packageId || null,
    algorithm,
    // The form is initialized to "x", but an operator may deliberately clear
    // it for a pool that requires an empty password.  Do not turn that valid
    // explicit value back into the default during serialization.
    pool_password: minerType === "seine" ? "" : $(`#${prefix}PoolPassword`).value,
    device_indices: minerAdapterHasCapability(minerType, "device_selection") ? parseDeviceIndices(`#${prefix}DeviceIndices`, `${label} GPU 索引`) : [],
    extra_config: extraConfig,
  };
}

async function saveFlightSheet() {
  const button = $("#createFlightButton");
  $("#flightError").textContent = "";
  if (state.flightHistoricalReadOnly) {
    $("#flightError").textContent = "历史飞行表为只读；请先明确点击“迁移为副本”。";
    return;
  }
  let primary;
  let secondary;
  let secondaryMode = "integrated";
  let gpuOc;
  try {
    primary = buildManagedLeg("flight", "主挖");
    secondary = null;
    if ($("#flightSecondaryEnabled").checked) {
      secondaryMode = flightSecondaryMode();
      secondary = buildManagedLeg("flightSecondary", "副挖");
      if (secondaryMode === "integrated") {
        secondary.miner_version = primary.miner_version;
        validateIntegratedDualFlight(primary, secondary);
      }
    }
    gpuOc = flightGpuOcIsSupported() ? collectFlightGpuOc() : null;
  } catch (error) {
    $("#flightError").textContent = error.message;
    return;
  }
  const current = beginResourceSave(button);
  if (!current) return;
  const editing = state.editingFlightSheetId;
  const migrating = Boolean(!editing && state.flightMigrationSourceId);
  try {
    const result = await api(editing ? `/api/flight-sheets/${encodeURIComponent(editing)}` : "/api/flight-sheets", {
      method: editing ? "PUT" : "POST",
      body: {
        name: $("#flightName").value.trim(),
        ...primary,
        worker_name_template: validateWorkerNameTemplate($("#flightWorker").value),
        gpu_oc: gpuOc,
        secondary_mode: secondaryMode,
        secondary,
      },
    });
    if (!current()) return;
    $("#flightDialog").close();
    state.editingFlightSheetId = null;
    state.flightMigrationSourceId = null;
    state.flightHistoricalReadOnly = false;
    toast(editing ? configurationSaveMessage("飞行表", result.reapply) : (migrating ? "迁移副本已创建；原飞行表保持不变" : "飞行表已保存"));
    await refreshAll(true);
  } catch (error) {
    if (current()) $("#flightError").textContent = error.message;
  } finally {
    if (current()) {
      button.disabled = false;
      button.textContent = state.flightMigrationSourceId ? "创建迁移副本" : (editing ? "保存修改" : "保存飞行表");
    }
  }
}

async function deleteResource(kind, id) {
  const resources = {
    wallet: { path: "/api/wallets", label: "钱包" },
    pool: { path: "/api/pools", label: "矿池" },
    minerPackage: { path: "/api/miner-packages", label: "矿工软件包" },
    flight: { path: "/api/flight-sheets", label: "飞行表" },
  };
  const resource = resources[kind];
  if (!resource || !window.confirm(`确认删除此${resource.label}？`)) return;
  try {
    await api(`${resource.path}/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`${resource.label}已删除`);
    await refreshAll(true);
  } catch (error) {
    toast(error.message, "error");
  }
}

function openDeploy(type) {
  const agent = state.selectedAgent;
  if (!agent || type !== "flight") return;
  const preparationReason = deviceRuntimePreparationReason(agent);
  if (preparationReason) { toast(preparationReason, "error"); return; }
  const profiles = state.flightSheets;
  if (!profiles.length) {
    toast("还没有可用的飞行表", "error");
    return;
  }
  state.selectedDeployType = type;
  state.deployAgentId = agent.id;
  $("#deployTitle").textContent = "应用飞行表";
  $("#deployDescription").textContent = `选择要下发到 ${agent.name} 的飞行表。第二矿工会按飞行表模式以内置双挖或独立进程启动；Agent 会再次检查软件包、资源隔离与额外字段。`;
  $("#deploySelectLabel").firstChild.textContent = "选择飞行表";
  $("#deploySelect").innerHTML = '<option value="">请明确选择飞行表</option>' + profiles.map((profile) => {
    const eligibility = flightTargetEligibility(agent, profile);
    return `<option value="${escapeHtml(profile.id)}"${eligibility.eligible ? "" : " disabled"}>${escapeHtml(profile.name)} · ${escapeHtml(profile.coin)}${profile.secondary ? ` + ${escapeHtml(profile.secondary.coin)}` : ""}${eligibility.reason ? ` · ${escapeHtml(eligibility.reason)}` : ""}</option>`;
  }).join("");
  $("#deploySelect").value = "";
  $("#deployWarning").classList.add("hidden");
  $("#deployError").textContent = "";
  syncDeploySelection();
  $("#deployDialog").showModal();
}

async function submitDeploy() {
  const agent = state.agents.find((item) => item.id === state.selectedAgent?.id) || state.selectedAgent;
  const type = state.selectedDeployType;
  const profileId = $("#deploySelect").value;
  if (!agent || type !== "flight" || !profileId) return;
  const sheet = state.flightSheets.find((item) => item.id === profileId);
  const versionIssue = flightSheetVersionIssue(sheet);
  if (versionIssue) {
    $("#deployError").textContent = `${versionIssue}。请先编辑飞行表并选择当前可用版本。`;
    return;
  }
  const eligibility = syncDeploySelection();
  if (!eligibility.eligible) { $("#deployError").textContent = eligibility.reason; return; }
  const button = $("#submitDeployButton");
  button.disabled = true;
  $("#deployError").textContent = "";
  try {
    await api(`/api/agents/${encodeURIComponent(agent.id)}/flight-sheets/${encodeURIComponent(profileId)}/apply`, { method: "POST" });
    $("#deployDialog").close();
    toast("飞行表任务已进入队列");
    closeDrawer();
    await refreshAll(true);
  } catch (error) {
    $("#deployError").textContent = error.message;
  } finally {
    syncDeploySelection();
  }
}

async function createUser() {
  const button = $("#createUserButton");
  $("#userError").textContent = "";
  button.disabled = true;
  try {
    await api("/api/users", { method: "POST", body: { username: $("#newUsername").value.trim(), password: $("#newPassword").value, role: $("#newRole").value } });
    $("#userDialog").close();
    toast("用户已创建");
    await refreshAll(true);
  } catch (error) {
    $("#userError").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  $("#loginForm").addEventListener("submit", login);
  $("#logoutButton").addEventListener("click", () => logout());
  $("#refreshButton").addEventListener("click", () => refreshAll());
  $("#clearAgentUpdateHistoryButton").addEventListener("click", clearAgentUpdateHistory);
  $("#drawerContent").addEventListener("input", (event) => {
    if (event.target.id === "agentDisplayName") event.target.dataset.dirty = "true";
  });
  $("#drawerContent").addEventListener("click", (event) => {
    const gpuButton = event.target.closest('[data-device-gpu-oc]');
    if (gpuButton && $("#drawerContent").contains(gpuButton)) {
      openDeviceGpuOc(Number(gpuButton.dataset.deviceGpuOc));
      return;
    }
    const commandButton = event.target.closest('[data-device-command-operation]');
    if (commandButton && $("#drawerContent").contains(commandButton)) {
      controlRemoteCommand(commandButton.dataset.commandId, commandButton.dataset.deviceCommandOperation, commandButton.dataset.commandName, commandButton);
    }
  });
  $$(".nav-item[data-page]").forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page)));
  $$('[data-page-link]').forEach((button) => button.addEventListener("click", () => showPage(button.dataset.pageLink)));
  $$('[data-overview-os]').forEach((button) => button.addEventListener("click", () => { state.overviewOsFilter = button.dataset.overviewOs; renderOverviewDevices(); }));
  $("#overviewDeviceSearch").addEventListener("input", renderOverviewDevices);
  $$('[data-open-enroll]').forEach((button) => button.addEventListener("click", openEnroll));
  $$('[data-close-dialog]').forEach((button) => button.addEventListener("click", () => button.closest("dialog")?.close()));
  $("#liveMinerLogDialog").addEventListener("close", stopLiveMinerLogs);
  $("#minerCacheForm").addEventListener("submit", cleanupMinerCache);
  $("#rescanMinerCacheButton").addEventListener("click", scanMinerCache);
  $("#closeMinerCacheDialogButton").addEventListener("click", closeMinerCacheCleanup);
  $("#minerCacheDialog").addEventListener("close", () => {
    state.minerCacheCleanupToken += 1;
    state.minerCacheCleanup = null;
  });
  $("#miningResidueForm").addEventListener("submit", cleanupMiningResidue);
  $("#rescanMiningResidueButton").addEventListener("click", scanMiningResidue);
  $("#closeMiningResidueDialogButton").addEventListener("click", closeMiningResidueRepair);
  $("#miningResidueDialog").addEventListener("cancel", (event) => {
    if (["cleaning", "restarting"].includes(state.miningResidueRepair?.phase)) {
      event.preventDefault();
      toast("修复任务正在设备上执行，完成前不能关闭此窗口", "error");
    }
  });
  $("#miningResidueDialog").addEventListener("close", () => {
    state.miningResidueRepairToken += 1;
    state.miningResidueRepair = null;
  });
  $("#enrollDialog").addEventListener("close", clearEnrollmentSecrets);
  $("#clearLiveMinerLogsButton").addEventListener("click", clearLiveMinerLogs);
  $("#liveMinerLogTabs").addEventListener("click", (event) => {
    const tab = event.target.closest("[data-tab]");
    const session = state.liveMinerLog;
    if (!tab || !session) return;
    session.activeTab = tab.dataset.tab || "all";
    renderLiveMinerLogs();
  });
  $("#enrollMode").addEventListener("change", updateEnrollmentMode);
  $("#replacementAgent").addEventListener("change", updateReplacementConfirmationHint);
  $("#enrollForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) generateEnrollmentToken(); });
  $("#actionForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) submitAction(); });
  $("#userForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) createUser(); });
  $("#walletForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) saveWallet(); });
  $("#poolForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) savePool(); });
  $("#poolDialog").addEventListener("close", () => { state.editingPoolId = null; });
  $("#minerPackageForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) createMinerPackage(); });
  $("#agentReleaseForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) uploadAgentRelease(); });
  $("#agentReleasePlatform").addEventListener("change", syncAgentReleasePlatform);
  $("#agentReleasePlatformFilter").addEventListener("change", renderAgentReleases);
  $("#agentUpdateForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) deployAgentRelease(); });
  $("#agentUpdateAgent").addEventListener("change", syncAgentUpdateSelection);
  $("#openAgentBatchUpdateButton").addEventListener("click", openAgentBatchUpdateDialog);
  $("#agentBatchUpdateRelease").addEventListener("change", renderAgentBatchUpdateAgents);
  $("#agentBatchUpdateForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) deployAgentReleaseBatch(); });
  $("#openFlightBatchApplyButton").addEventListener("click", openFlightBatchApplyDialog);
  $("#flightBatchApplySheet").addEventListener("change", renderFlightBatchApplyAgents);
  $("#flightBatchApplyForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) applyFlightSheetBatch(); });
  $("#openMiningStopBatchButton").addEventListener("click", () => openMiningControlBatchDialog("stop_mining"));
  $("#openMiningRestartBatchButton").addEventListener("click", () => openMiningControlBatchDialog("restart_mining"));
  $("#miningControlBatchForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) submitMiningControlBatch(); });
  $("#flightForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (flightMinerConfigIsOpen()) {
      if (!validateActiveFlightMinerConfig(event.currentTarget)) return;
      closeFlightMinerConfig();
      return;
    }
    if (validateFlightFormForSave(event.currentTarget)) saveFlightSheet();
  });
  $$('[data-flight-configure]').forEach((button) => button.addEventListener("click", () => openFlightMinerConfig(button.dataset.flightConfigure)));
  $$('[data-flight-config-close]').forEach((button) => button.addEventListener("click", closeFlightMinerConfig));
  $$('[data-flight-config-reset]').forEach((button) => button.addEventListener("click", resetActiveFlightMinerConfig));
  $("#flightForm").addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !flightMinerConfigIsOpen()) return;
    event.preventDefault();
    event.stopPropagation();
    closeFlightMinerConfig();
  });
  $("#migrateFlightButton").addEventListener("click", beginFlightMigrationCopy);
  $("#flightDialog").addEventListener("close", () => {
    closeFlightMinerConfig({ restoreFocus: false });
    state.editingFlightSheetId = null;
    state.flightMigrationSourceId = null;
    state.flightHistoricalReadOnly = false;
  });
  $("#flightDialog").addEventListener("cancel", (event) => {
    if (!flightMinerConfigIsOpen()) return;
    event.preventDefault();
    closeFlightMinerConfig();
  });
  $("#deviceGpuOcForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) applyDeviceGpuOc(); });
  $("#deleteDeviceGpuOcButton").addEventListener("click", deleteDeviceGpuOc);
  $("#deviceGpuOcDialog").addEventListener("close", (event) => { if (!event.currentTarget.open) state.deviceGpuOcTarget = null; });
  $$("[data-device-gpu-batch-reset]").forEach((input) => input.addEventListener("change", syncDeviceGpuOcBatchSelection));
  $("#deviceGpuOcBatchForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) applyDeviceGpuOcBatch(); });
  $("#clearDeviceGpuOcBatchButton").addEventListener("click", clearDeviceGpuOcBatch);
  $("#deviceGpuOcBatchDialog").addEventListener("close", (event) => { if (!event.currentTarget.open) state.deviceGpuOcBatchTarget = null; });
  $("#deployForm").addEventListener("submit", (event) => { event.preventDefault(); if (event.currentTarget.reportValidity()) submitDeploy(); });
  $("#deploySelect").addEventListener("change", syncDeploySelection);
  $("#addWalletButton").addEventListener("click", () => openWalletDialog());
  $("#addPoolButton").addEventListener("click", () => openPoolDialog());
  $("#addMinerPackageButton").addEventListener("click", openMinerPackageDialog);
  $("#addAgentReleaseButton").addEventListener("click", openAgentReleaseDialog);
  $$('input[name="minerPackageSource"]').forEach((radio) => radio.addEventListener("change", () => syncMinerPackageSource(radio.value)));
  $("#minerPackageManagedType").addEventListener("change", () => syncMinerPackageClassification());
  $("#minerPackageManagedOsFamily").addEventListener("change", () => syncMinerPackageClassification());
  $("#minerPackageDualPlatform").addEventListener("change", () => syncMinerPackageClassification({ syncArchive: false }));
  $("#minerPackageFile").addEventListener("change", (event) => {
    if ($("#minerPackageManagedType").value) {
      syncMinerPackageClassification();
      return;
    }
    const filename = event.currentTarget.files[0]?.name.toLowerCase() || "";
    if (filename.endsWith(".zip")) $("#minerPackageArchiveType").value = "zip";
    if (filename.endsWith(".tar.gz") || filename.endsWith(".tgz")) $("#minerPackageArchiveType").value = "tar.gz";
  });
  $("#addFlightButton").addEventListener("click", openFlightDialog);
  $("#flightMinerType").addEventListener("change", (event) => {
    resetAdapterSchemaOptionValues("flight");
    const pool = $("#flightPool");
    if (event.currentTarget.value === "pearlminer") {
      if (!pearlMinerPoolUrlSupported(pool.value)) pool.value = "pearl://global.pearlfortune.org:443";
    } else if (pool.value.trim().startsWith("pearl://")) {
      pool.value = "";
    }
    syncManagedLeg("flight", { resetVersion: true });
    if ($("#flightSecondaryEnabled").checked) syncSecondaryMode({ preserveAlgorithm: true });
  });
  for (const prefix of ["flight", "flightSecondary"]) {
    $(`#${prefix}NativePackageId`).addEventListener("change", () => renderManagedVersionCompatibility(prefix));
    for (const suffix of ["Pool", "PoolPassword"]) $(`#${prefix}${suffix}`).addEventListener("input", (event) => {
      if (suffix === "PoolPassword") event.target.dataset.dirty = "true";
      syncManagedProtocolUI(prefix);
    });
    adapterOptionContainer(prefix)?.addEventListener("input", () => { preserveRenderedAdapterOptionValues(prefix); syncManagedProtocolUI(prefix); });
    adapterOptionContainer(prefix)?.addEventListener("change", (event) => {
      preserveRenderedAdapterOptionValues(prefix);
      if (event.target.dataset.adapterOption === "quic_wallet_mode" && event.target.value === "token") {
        const password = $(`#${prefix}PoolPassword`);
        if (password && password.dataset.dirty !== "true" && password.value === "x") password.value = "";
      }
      if (event.target.dataset.adapterOption === "resource_kind") {
        syncManagedExecutionMode(prefix); syncCpuRandomxFields(prefix); syncFlightGpuOcVisibility();
      }
      syncManagedProtocolUI(prefix);
    });
  }
  $("#flightMinerVersion").addEventListener("change", () => {
    syncManagedLeg("flight", { preserveAlgorithm: true });
    renderManagedVersionCompatibility("flight");
    syncSecondaryMinerVersion();
  });
  $("#flightSecondaryMinerVersion").addEventListener("change", () => {
    syncManagedLeg("flightSecondary", { preserveAlgorithm: true });
    renderManagedVersionCompatibility("flightSecondary");
  });
  $("#flightExecutionMode").addEventListener("change", () => syncManagedLeg("flight", { preserveAlgorithm: true }));
  $("#flightSecondaryExecutionMode").addEventListener("change", () => syncManagedLeg("flightSecondary", { preserveAlgorithm: true }));
  $("#flightSecondaryMinerType").addEventListener("change", (event) => {
    resetAdapterSchemaOptionValues("flightSecondary");
    const pool = $("#flightSecondaryPool");
    if (event.currentTarget.value === "pearlminer") {
      if (!pearlMinerPoolUrlSupported(pool.value)) pool.value = "pearl://global.pearlfortune.org:443";
    } else if (pool.value.trim().startsWith("pearl://")) {
      pool.value = "";
    }
    syncManagedLeg("flightSecondary", { resetVersion: true });
  });
  $("#flightWallet").addEventListener("change", () => { syncManagedLeg("flight"); populateSavedPoolSelectors(); });
  $("#flightSecondaryWallet").addEventListener("change", () => { syncManagedLeg("flightSecondary"); populateSavedPoolSelectors(); });
  $("#flightSavedPool").addEventListener("change", () => applySavedPoolToFlight("flight"));
  $("#flightSecondarySavedPool").addEventListener("change", () => applySavedPoolToFlight("flightSecondary"));
  for (const selector of ["#flightPool", "#flightPoolPassword"]) {
    $(selector).addEventListener("input", () => { $("#flightSavedPool").value = ""; });
  }
  for (const selector of ["#flightSecondaryPool", "#flightSecondaryPoolPassword"]) {
    $(selector).addEventListener("input", () => { $("#flightSecondarySavedPool").value = ""; });
  }
  $("#flightGpuBackend").addEventListener("change", () => syncManagedLeg("flight", { preserveAlgorithm: true }));
  $("#flightSecondaryGpuBackend").addEventListener("change", () => syncManagedLeg("flightSecondary", { preserveAlgorithm: true }));
  $("#flightAlgorithm").addEventListener("change", () => {
    syncManagedExecutionMode("flight");
    syncCpuRandomxFields("flight");
    renderManagedVersionCompatibility("flight");
    syncFlightGpuOcVisibility();
    syncManagedProtocolUI("flight");
  });
  $("#flightSecondaryAlgorithm").addEventListener("change", () => {
    syncManagedExecutionMode("flightSecondary");
    syncCpuRandomxFields("flightSecondary");
    renderManagedVersionCompatibility("flightSecondary");
    syncFlightGpuOcVisibility();
    syncManagedProtocolUI("flightSecondary");
  });
  $("#flightDeviceIndices").addEventListener("input", updateFlightGpuOcIndexHints);
  $("#flightSecondaryDeviceIndices").addEventListener("input", updateFlightGpuOcIndexHints);
  $("#addFlightGpuOcRowButton").addEventListener("click", () => addFlightGpuOcRow());
  $("#flightSecondaryEnabled").addEventListener("change", (event) => {
    setSecondaryEnabled(event.currentTarget.checked);
    if (event.currentTarget.checked) syncSecondaryMode({ resetVersion: true });
    else syncSecondaryMode();
  });
  $("#flightSecondaryMode").addEventListener("change", () => syncSecondaryMode({ resetVersion: true }));
  $("#workloadRangeForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.currentTarget.reportValidity()) refreshWorkloadSummary();
  });
  $$('[data-workload-preset]').forEach((button) => button.addEventListener("click", () => {
    setWorkloadPreset(Number(button.dataset.workloadPreset), true);
  }));
  for (const selector of ["#workloadFrom", "#workloadTo"]) {
    $(selector).addEventListener("input", () => $$('[data-workload-preset]').forEach((button) => button.classList.remove("active")));
  }
  $$('[data-workload-coin-mode]').forEach((button) => button.addEventListener("click", () => setWorkloadCoinMode(button.dataset.workloadCoinMode)));
  $("#workloadOwnerAdd")?.addEventListener("click", () => beginWorkloadOwnerEdit());
  $("#workloadOwnerName")?.addEventListener("input", (event) => { if (state.workloadOwnerDraft) state.workloadOwnerDraft.name = event.target.value; });
  $("#workloadOwnerForm")?.addEventListener("submit", (event) => { event.preventDefault(); saveWorkloadOwner(); });
  $("#workloadOwnerCancel")?.addEventListener("click", cancelWorkloadOwnerEdit);
  $("#workloadOwnerReload")?.addEventListener("click", reloadSavedWorkloadOwners);
  $("#workloadOwnerImport")?.addEventListener("click", importLegacyWorkloadOwners);
  $("#workloadRevenueTotal").addEventListener("input", rememberWorkloadRevenueTotal);
  $("#workloadSettlementNote").addEventListener("input", () => {
    state.settlementNoteDirty = true;
    state.settlementNoteError = "";
    state.settlementNoteNotice = "";
    renderWorkloadSettlementNote();
  });
  $("#workloadSettlementUseAsStart").addEventListener("click", useSavedSettlementAsWorkloadStart);
  $("#workloadSettlementNoteForm").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.currentTarget.reportValidity()) saveWorkloadSettlementNote();
  });
  setWorkloadPreset(24, false);
  $("#deviceSearch").addEventListener("input", renderDevices);
  $("#clearConfigurationRefreshBadgesButton").addEventListener("click", dismissAllConfigurationRefreshes);
  $$(".filter-tab[data-filter]").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    $$(".filter-tab[data-filter]").forEach((item) => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-pressed", String(active)); });
    renderDevices();
  }));
  $("#backdrop").addEventListener("click", closeDrawer);
  $("[data-close-drawer]").addEventListener("click", closeDrawer);
  $("#copyTokenButton").addEventListener("click", copyToken);
  $("#addUserButton").addEventListener("click", () => {
    $("#newUsername").value = ""; $("#newPassword").value = ""; $("#userError").textContent = ""; $("#userDialog").showModal();
  });
  $("#changePasswordButton").addEventListener("click", openPasswordChange);
  $("#passwordChangeForm").addEventListener("submit", changeOwnPassword);
  $("#passwordChangeDialog").addEventListener("cancel", (event) => {
    if (state.passwordChangeRequest) event.preventDefault();
  });
  $("#passwordChangeDialog").addEventListener("close", () => {
    if (!$("#passwordChangeDialog").open) clearPasswordChangeForm();
  });
  $("#openSetupButton").addEventListener("click", openSetupWizard);
  $("#setupPasswordButton").addEventListener("click", openPasswordChange);
  $("#setupEnrollmentButton").addEventListener("click", openEnroll);
  $$("[data-setup-later]").forEach((button) => button.addEventListener("click", () => updateSetupState("later")));
  $$("[data-setup-step]").forEach((button) => button.addEventListener("click", () => updateSetupState("step", button.dataset.setupStep)));
  $$("[data-setup-page]").forEach((button) => button.addEventListener("click", () => openSetupPage(button.dataset.setupPage)));
  $("#setupPreviousButton").addEventListener("click", () => updateSetupState("step", state.setupState?.step === "configuration" ? "device" : "password"));
  $("#setupNextButton").addEventListener("click", () => updateSetupState(state.setupState?.step === "configuration" ? "complete" : "skip"));
  $("#setupDialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    if (!state.setupLoading && !state.setupSaving) void updateSetupState("later");
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
}

async function boot() {
  bindEvents();
  $$(".filter-tab[data-filter]").forEach((item) => item.setAttribute("aria-pressed", String(item.classList.contains("active"))));
  const refreshVisibleApp = () => {
    if (document.hidden || $("#appView").classList.contains("hidden")) return;
    refreshAll(true);
    const agent = state.selectedAgent;
    if (agent && $("#deviceDrawer")?.classList.contains("open") && state.deviceDetailTab === "logs") {
      loadDeviceActivity(agent.id, { force: true });
    }
  };
  setInterval(refreshVisibleApp, 10000);
  document.addEventListener("visibilitychange", refreshVisibleApp);
  if (!state.token) { loginView(true); return; }
  // The login form is initially visible while a stored session is restored.
  // Keep that restoration and an interactive sign-in from racing each other.
  const button = $("#loginForm button[type=submit]");
  button.disabled = true;
  try {
    await enterApp();
  } catch {
    logout(false);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", boot);
