[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$AgentExePath,

    [string]$PythonPath,
    [string]$ProjectRoot,

    [string]$ExpectedVersion,
    [int]$Port = 18081
)

$ErrorActionPreference = "Stop"

# Some Windows hosts expose both
# ``Path`` and ``PATH`` in the process environment. Windows itself treats the
# names case-insensitively, but Windows PowerShell 5.1's Start-Process tries to
# copy them into a case-insensitive dictionary and aborts on the duplicate.
# Keep the canonical ``Path`` entry so every child process gets the same value.
$ProcessPathNames = @(
    [Environment]::GetEnvironmentVariables("Process").Keys |
        Where-Object { [string]$_ -ieq "Path" }
)
if ($ProcessPathNames.Count -gt 1 -and $ProcessPathNames -contains "Path") {
    foreach ($PathName in $ProcessPathNames) {
        if ([string]$PathName -cne "Path") {
            Remove-Item -LiteralPath "Env:$PathName" -ErrorAction SilentlyContinue
        }
    }
}

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
}
else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$AgentExePath = (Resolve-Path -LiteralPath $AgentExePath).Path
if (-not $PSBoundParameters.ContainsKey("ExpectedVersion")) {
    # Bind the default to the selected source, not the executable under test.
    # Read that exact version file with isolated Python, avoiding caller imports.
    $SourceVersionOutput = @(& $PythonPath -I -c "import runpy, sys; from pathlib import Path; print(runpy.run_path(str(Path(sys.argv[1]) / 'agent' / 'version.py'))['__version__'])" $ProjectRoot)
    if ($LASTEXITCODE -ne 0 -or $SourceVersionOutput.Count -ne 1 -or
        ([string]$SourceVersionOutput[0]).Trim() -notmatch '^\d+\.\d+\.\d+$') {
        throw "无法读取指定项目的 Agent 源码版本。"
    }
    $ExpectedVersion = ([string]$SourceVersionOutput[0]).Trim()
}

$WorkspaceRoot = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "..")).Path
$WorkParent = [IO.Path]::GetFullPath((Join-Path $WorkspaceRoot "work"))
$SmokeRoot = [IO.Path]::GetFullPath((Join-Path $WorkParent ("windows-agent-smoke." + [Guid]::NewGuid().ToString("N"))))
$ExpectedPrefix = $WorkParent.TrimEnd('\') + '\'
if (-not $SmokeRoot.StartsWith($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "冒烟测试目录必须位于 workspace\work 下。"
}
if (Test-Path -LiteralPath $SmokeRoot) {
    throw "冒烟测试目录发生冲突，拒绝复用：$SmokeRoot"
}
New-Item -ItemType Directory -Path $SmokeRoot | Out-Null

$EnvironmentNames = @(
    "RIGPILOT_DATA_DIR",
    "RIGPILOT_ADMIN_USERNAME",
    "RIGPILOT_ADMIN_PASSWORD",
    "RIGPILOT_SECRET_KEY",
    "RIGPILOT_REQUIRE_HTTPS",
    "RIGPILOT_AGENT_ONLINE_SECONDS",
    "RIGPILOT_ENROLLMENT_TOKEN",
    "RIGPILOT_REPLACEMENT_TOKEN"
)
$PreviousEnvironment = @{}
foreach ($Name in $EnvironmentNames) {
    $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
}

$DataDir = Join-Path $SmokeRoot "server-data"
$ConfigPath = Join-Path $SmokeRoot "agent-config.json"
$StatePath = Join-Path $SmokeRoot "agent-state.json"
$AgentLogPath = Join-Path $SmokeRoot "agent.jsonl"
$ServerOutputPath = Join-Path $SmokeRoot "server.stdout.log"
$ServerErrorPath = Join-Path $SmokeRoot "server.stderr.log"
$AdminPassword = "Smoke-" + [Guid]::NewGuid().ToString("N")
$Server = $null
$Result = $null
$PreserveSmokeRoot = $false

function Invoke-FrozenAgentOnce {
    param(
        [Parameter(Mandatory = $true)][string]$RunName,
        [string]$ConfigFilePath = $ConfigPath
    )

    $OutputPath = Join-Path $SmokeRoot "$RunName.stdout.log"
    $ErrorPath = Join-Path $SmokeRoot "$RunName.stderr.log"
    $Process = Start-Process `
        -FilePath $AgentExePath `
        -ArgumentList @("--config", "`"$ConfigFilePath`"", "--once") `
        -WorkingDirectory (Split-Path -Parent $AgentExePath) `
        -WindowStyle Hidden `
        -RedirectStandardOutput $OutputPath `
        -RedirectStandardError $ErrorPath `
        -PassThru
    # Keep the original handle so PowerShell 5.1 can read the exit code after waiting.
    $null = $Process.Handle
    if (-not $Process.WaitForExit(60000)) {
        # Kill the original process object, never look up a possibly reused
        # PID. PowerShell 5.1 cannot prove that a onefile bootloader's child
        # tree exited, so retain evidence even when this parent exits.
        $script:PreserveSmokeRoot = $true
        try {
            $Process.Kill()
            [void]$Process.WaitForExit(5000)
        }
        catch { }
        throw "冻结 Agent 单次冒烟运行超过 60 秒：$RunName；子进程退出未确认，保留目录：$SmokeRoot"
    }
    $Process.Refresh()
    if ($Process.ExitCode -ne 0) {
        $Details = if (Test-Path -LiteralPath $ErrorPath) {
            Get-Content -LiteralPath $ErrorPath -Raw
        }
        else {
            "没有错误日志"
        }
        foreach ($SecretName in @("RIGPILOT_ENROLLMENT_TOKEN", "RIGPILOT_REPLACEMENT_TOKEN")) {
            $Secret = [Environment]::GetEnvironmentVariable($SecretName, "Process")
            if ($Secret) {
                $Details = $Details.Replace($Secret, "[REDACTED]")
            }
        }
        throw "冻结 Agent 运行失败，退出码 $($Process.ExitCode)：$Details"
    }
}

try {
    $env:RIGPILOT_DATA_DIR = $DataDir
    $env:RIGPILOT_ADMIN_USERNAME = "admin"
    $env:RIGPILOT_ADMIN_PASSWORD = $AdminPassword
    $env:RIGPILOT_SECRET_KEY = "SmokeSecret-" + [Guid]::NewGuid().ToString("N")
    $env:RIGPILOT_REQUIRE_HTTPS = "0"
    $env:RIGPILOT_AGENT_ONLINE_SECONDS = "5"
    [Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $null, "Process")
    [Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $null, "Process")

    $Server = Start-Process `
        -FilePath $PythonPath `
        -ArgumentList @(
            "-m", "uvicorn", "server.main:app",
            "--host", "127.0.0.1",
            "--port", [string]$Port,
            "--log-level", "warning"
        ) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ServerOutputPath `
        -RedirectStandardError $ServerErrorPath `
        -PassThru

    $BaseUrl = "http://127.0.0.1:$Port"
    $Healthy = $false
    for ($Attempt = 0; $Attempt -lt 100; $Attempt++) {
        if ($Server.HasExited) {
            throw "临时控制端提前退出，退出码 $($Server.ExitCode)。"
        }
        try {
            $Health = Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 1
            if ($Health.status -eq "ok") {
                $Healthy = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $Healthy) {
        throw "临时控制端未能在 10 秒内启动。"
    }

    $LoginBody = @{ username = "admin"; password = $AdminPassword } | ConvertTo-Json
    $Session = Invoke-RestMethod `
        -Uri "$BaseUrl/api/auth/login" `
        -Method Post `
        -ContentType "application/json" `
        -Body $LoginBody
    $Headers = @{ Authorization = "Bearer $($Session.token)" }
    $EnrollmentBody = @{ label = "windows-installer-smoke"; ttl_minutes = 5 } |
        ConvertTo-Json
    $Enrollment = Invoke-RestMethod `
        -Uri "$BaseUrl/api/enrollment-tokens" `
        -Method Post `
        -Headers $Headers `
        -ContentType "application/json" `
        -Body $EnrollmentBody

    @{
        server_url = $BaseUrl
        state_file = $StatePath
        name = "windows-installer-smoke"
        heartbeat_interval = 30
        request_timeout = 5
        log_level = "INFO"
        log_file = $AgentLogPath
        allow_reboot = $false
        allow_shutdown = $false
        allow_miner_downloads = $false
        allow_gpu_oc = $false
    } | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8

    $env:RIGPILOT_ENROLLMENT_TOKEN = $Enrollment.token
    Invoke-FrozenAgentOnce -RunName "first-enrollment"
    [Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $null, "Process")
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        throw "冻结 Agent 未生成设备状态文件。"
    }

    Invoke-FrozenAgentOnce -RunName "second-heartbeat"

    $Agents = @(
        Invoke-RestMethod -Uri "$BaseUrl/api/agents" -Headers $Headers
    )
    if ($Agents.Count -ne 1) {
        throw "临时控制端应只有一个 Agent，实际为 $($Agents.Count)。"
    }
    if ($Agents[0].agent_version -ne $ExpectedVersion) {
        throw "控制端收到的 Agent 版本不正确：$($Agents[0].agent_version)"
    }
    if ($Agents[0].name -ne "windows-installer-smoke") {
        throw "控制端收到的 Agent 名称不正确：$($Agents[0].name)"
    }
    $ExpectedProcessor = [string](Get-ItemPropertyValue `
        -LiteralPath "Registry::HKEY_LOCAL_MACHINE\HARDWARE\DESCRIPTION\System\CentralProcessor\0" `
        -Name "ProcessorNameString" `
        -ErrorAction Stop)
    $ExpectedProcessor = ($ExpectedProcessor -replace '\s+', ' ').Trim()
    $ReportedProcessor = ([string]$Agents[0].inventory.processor -replace '\s+', ' ').Trim()
    if ([string]::IsNullOrWhiteSpace($ExpectedProcessor) -or
        $ReportedProcessor -cne $ExpectedProcessor) {
        throw "控制端收到的 CPU 型号不正确：$ReportedProcessor"
    }
    $NetworkInventory = $Agents[0].inventory.network
    if ($null -eq $NetworkInventory -or
        [int]$NetworkInventory.schema_version -ne 1 -or
        $null -eq $NetworkInventory.local_ip_addresses -or
        $null -eq $NetworkInventory.public_ip_addresses) {
        throw "控制端未收到冻结 Agent 的公网/局域网分类结果。"
    }
    if ($null -eq $Agents[0].local_ip_addresses) {
        throw "控制端未保存冻结 Agent 的局域网地址字段。"
    }

    # Exercise a second first contact with the actual frozen EXE. The first
    # Agent has exited; the short local-only online window makes it offline
    # without altering Controller storage or touching a real device.
    $OldState = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $OriginalAgentId = [string]$Agents[0].id
    if (-not $OriginalAgentId -or $OldState.agent_id -ne $OriginalAgentId) {
        throw "首次注册的本地状态与控制端设备 ID 不一致。"
    }
    Start-Sleep -Seconds 6
    $OfflineAgents = @(Invoke-RestMethod -Uri "$BaseUrl/api/agents" -Headers $Headers)
    if ($OfflineAgents.Count -ne 1 -or $OfflineAgents[0].status -ne "offline") {
        throw "本地旧 Agent 尚未离线，拒绝启动继承冒烟测试。"
    }
    $ReplacementIssue = Invoke-RestMethod `
        -Uri "$BaseUrl/api/agents/$OriginalAgentId/replacement-token" `
        -Method Post `
        -Headers $Headers `
        -ContentType "application/json" `
        -Body (@{ confirm_name = "windows-installer-smoke"; ttl_minutes = 5 } | ConvertTo-Json)
    if ($ReplacementIssue.schema -ne "rigpilot.replacement.v1" -or
        $ReplacementIssue.agent_id -ne $OriginalAgentId) {
        throw "临时控制端未签发绑定到原设备的继承码。"
    }
    $ReplacementConfigPath = Join-Path $SmokeRoot "replacement-config.json"
    $ReplacementStatePath = Join-Path $SmokeRoot "replacement-state.json"
    $ReplacementConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ReplacementConfig.state_file = $ReplacementStatePath
    $ReplacementConfig.name = "windows-replacement-smoke"
    $ReplacementConfig.log_file = Join-Path $SmokeRoot "replacement-agent.jsonl"
    $ReplacementConfig | ConvertTo-Json | Set-Content -LiteralPath $ReplacementConfigPath -Encoding UTF8
    $env:RIGPILOT_REPLACEMENT_TOKEN = $ReplacementIssue.token
    Invoke-FrozenAgentOnce -RunName "first-inheritance" -ConfigFilePath $ReplacementConfigPath
    [Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $null, "Process")
    if (-not (Test-Path -LiteralPath $ReplacementStatePath -PathType Leaf)) {
        throw "冻结 Agent 未生成继承后的设备状态文件。"
    }
    $InheritedState = Get-Content -LiteralPath $ReplacementStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($InheritedState.agent_id -ne $OriginalAgentId -or
        -not $InheritedState.agent_secret -or
        $InheritedState.agent_secret -eq $OldState.agent_secret) {
        throw "冻结 Agent 未保留原设备 ID 并轮换密钥。"
    }
    Invoke-FrozenAgentOnce -RunName "inherited-heartbeat" -ConfigFilePath $ReplacementConfigPath
    $InheritedAgents = @(Invoke-RestMethod -Uri "$BaseUrl/api/agents" -Headers $Headers)
    if ($InheritedAgents.Count -ne 1 -or
        $InheritedAgents[0].id -ne $OriginalAgentId -or
        $InheritedAgents[0].name -ne "windows-installer-smoke" -or
        $InheritedAgents[0].agent_version -ne $ExpectedVersion) {
        throw "继承后控制端未保留同一设备身份、名称和版本。"
    }
    if ($Server -and -not $Server.HasExited) {
        Stop-Process -Id $Server.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $Server.Id -Timeout 5 -ErrorAction SilentlyContinue
    }
    $PrivateValues = @(
        $Enrollment.token,
        $ReplacementIssue.token,
        $OldState.agent_secret,
        $InheritedState.agent_secret
    )
    foreach ($LogFile in @(Get-ChildItem -LiteralPath $SmokeRoot -File |
        Where-Object { $_.Extension -in @(".log", ".jsonl") })) {
        $LogContents = [IO.File]::ReadAllText($LogFile.FullName, [Text.Encoding]::UTF8)
        foreach ($PrivateValue in $PrivateValues) {
            if ($PrivateValue -and $LogContents.Contains([string]$PrivateValue)) {
                throw "本地冒烟日志包含设备凭据：$($LogFile.Name)"
            }
        }
    }

    # A separate loopback-only controller drops the first accepted inheritance
    # response. The actual frozen EXE must retain the pending nonce, reject a
    # different code locally, then replay the same request and heartbeat.
    $RetryScript = Join-Path $ProjectRoot "deploy\windows\smoke-inheritance-retry.py"
    if (-not (Test-Path -LiteralPath $RetryScript -PathType Leaf)) {
        throw "冻结继承重试冒烟脚本缺失。"
    }
    $RetryReport = Join-Path $SmokeRoot "inheritance-retry-report.json"
    $RetryWorkDir = Join-Path $SmokeRoot "inheritance-retry"
    & $PythonPath -B $RetryScript --agent $AgentExePath --work-dir $RetryWorkDir --expected-version $ExpectedVersion --report $RetryReport
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $RetryReport -PathType Leaf)) {
        throw "冻结 Agent 继承响应丢失重试冒烟失败。"
    }
    $Retry = Get-Content -LiteralPath $RetryReport -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Retry.passed -ne $true -or $Retry.claim_attempts -ne 2 -or
        $Retry.same_request_and_secret_on_retry -ne $true -or
        $Retry.different_code_rejected_before_post -ne $true -or
        $Retry.authenticated_heartbeats -ne 2 -or
        $Retry.existing_state_no_new_claim -ne $true) {
        throw "冻结 Agent 继承重试回执不完整。"
    }

    $Result = [PSCustomObject]@{
        FrozenAgent = $AgentExePath
        Enrollment = "passed"
        SubsequentHeartbeat = "passed"
        DeviceInheritance = "passed"
        InheritedHeartbeat = "passed"
        InheritanceRetry = "passed"
        OriginalAgentId = $OriginalAgentId
        ReportedVersion = $Agents[0].agent_version
        ReportedProcessor = $ReportedProcessor
        ReportedPublicIp = $Agents[0].public_ip
        ReportedLocalIps = @($Agents[0].local_ip_addresses) -join ", "
    }
}
finally {
    [Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $null, "Process")
    [Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $null, "Process")
    if ($Server -and -not $Server.HasExited) {
        Stop-Process -Id $Server.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $Server.Id -Timeout 5 -ErrorAction SilentlyContinue
    }
    foreach ($Name in $EnvironmentNames) {
        [Environment]::SetEnvironmentVariable(
            $Name,
            $PreviousEnvironment[$Name],
            "Process"
        )
    }
    if ($PreserveSmokeRoot) {
        Write-Warning "冒烟进程树退出未确认，未删除测试目录：$SmokeRoot"
    }
    elseif (Test-Path -LiteralPath $SmokeRoot) {
        $Item = Get-Item -LiteralPath $SmokeRoot -Force
        if (-not ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Remove-Item -LiteralPath $SmokeRoot -Recurse -Force
        }
    }
}

$Result | Format-List
