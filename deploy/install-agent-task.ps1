[CmdletBinding(DefaultParameterSetName = "Frozen")]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true, ParameterSetName = "Frozen")]
    [string]$AgentExePath,

    [Parameter(Mandatory = $true, ParameterSetName = "Source")]
    [string]$ProjectRoot,

    [Parameter(ParameterSetName = "Source")]
    [string]$PythonPath = "python.exe",

    [string]$ExpectedExistingAgentExePath,
    [string]$ExpectedExistingProjectRoot,
    [string]$ExpectedExistingPythonPath,
    [string]$ExpectedExistingConfigPath
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "请在管理员 PowerShell 中运行此脚本。"
}

$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$IsSourceInstall = $PSCmdlet.ParameterSetName -eq "Source"
if ($PSCmdlet.ParameterSetName -eq "Frozen") {
    $AgentExePath = (Resolve-Path -LiteralPath $AgentExePath).Path
    if ([IO.Path]::GetExtension($AgentExePath) -ne ".exe") {
        throw "AgentExePath 必须指向冻结后的 EXE。"
    }
    $Execute = $AgentExePath
    $Arguments = "--config `"$ConfigPath`""
    $WorkingDirectory = Split-Path -Parent $AgentExePath
}
else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $Execute = (Get-Command -Name $PythonPath -CommandType Application -ErrorAction Stop).Source
    $Arguments = "-m agent --config `"$ConfigPath`""
    $WorkingDirectory = $ProjectRoot
}

$TaskPath = "\"
$TaskName = "RigPilotAgent"
$KnownDescription = "RigPilot outbound-only device monitoring agent"
$ExistingTask = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    $ExistingActions = @($ExistingTask.Actions)
    if ($ExistingActions.Count -ne 1) {
        throw "A same-name scheduled task has multiple actions; refusing to replace it."
    }
    $ExistingExecute = [Environment]::ExpandEnvironmentVariables(
        [string]$ExistingActions[0].Execute
    )
    $ExistingArguments = [string]$ExistingActions[0].Arguments
    $ExistingWorkingDirectory = [Environment]::ExpandEnvironmentVariables(
        [string]$ExistingActions[0].WorkingDirectory
    )
    $HasKnownDescription = [string]::Equals(
            [string]$ExistingTask.Description,
            $KnownDescription,
            [StringComparison]::Ordinal
        )
    # Older source installs stored the caller's exact Python command. Its
    # current root/config context still proves ownership during a normal upgrade.
    $HasExpectedExecute = [string]::Equals($ExistingExecute, $Execute, [StringComparison]::OrdinalIgnoreCase) -or
        ($IsSourceInstall -and
         [string]::Equals($ExistingExecute, $PythonPath, [StringComparison]::OrdinalIgnoreCase))
    $IsExpectedRigPilotTask = $HasKnownDescription -and $HasExpectedExecute -and
        [string]::Equals($ExistingArguments, $Arguments, [StringComparison]::Ordinal) -and
        [string]::Equals($ExistingWorkingDirectory, $WorkingDirectory, [StringComparison]::OrdinalIgnoreCase)
    $IsReplaceableRigPilotTask = $false
    if ($ExpectedExistingAgentExePath -or $ExpectedExistingProjectRoot -or
        $ExpectedExistingPythonPath -or $ExpectedExistingConfigPath) {
        if (-not $ExpectedExistingConfigPath -or
            ([bool]$ExpectedExistingAgentExePath -eq [bool]$ExpectedExistingProjectRoot) -or
            ($ExpectedExistingProjectRoot -and -not $ExpectedExistingPythonPath) -or
            ($ExpectedExistingAgentExePath -and $ExpectedExistingPythonPath)) {
            throw "旧任务迁移需要完整且唯一的 EXE 或源码执行上下文及 ExpectedExistingConfigPath。"
        }
        $OldConfig = [IO.Path]::GetFullPath($ExpectedExistingConfigPath)
        if ($ExpectedExistingAgentExePath) {
            $OldExecute = [IO.Path]::GetFullPath($ExpectedExistingAgentExePath)
            $OldArguments = "--config `"$OldConfig`""
            $OldWorkingDirectory = Split-Path -Parent $OldExecute
        }
        else {
            $OldExecute = [Environment]::ExpandEnvironmentVariables($ExpectedExistingPythonPath)
            $OldArguments = "-m agent --config `"$OldConfig`""
            $OldWorkingDirectory = [IO.Path]::GetFullPath($ExpectedExistingProjectRoot)
        }
        # A legacy blank description is accepted only with this explicitly
        # supplied complete action context, never as ownership by itself.
        $IsReplaceableRigPilotTask =
            ($HasKnownDescription -or [string]::IsNullOrWhiteSpace([string]$ExistingTask.Description)) -and
            [string]::Equals($ExistingExecute, $OldExecute, [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals($ExistingArguments, $OldArguments, [StringComparison]::Ordinal) -and
            [string]::Equals($ExistingWorkingDirectory, $OldWorkingDirectory, [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $IsExpectedRigPilotTask -and -not $IsReplaceableRigPilotTask) {
        throw "已存在同名但执行动作不属于 RigPilot 的计划任务，拒绝覆盖：$ExistingExecute"
    }
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

$Action = New-ScheduledTaskAction `
    -Execute $Execute `
    -Argument $Arguments `
    -WorkingDirectory $WorkingDirectory
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskPath $TaskPath `
    -TaskName $TaskName `
    -Description $KnownDescription `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
Write-Host "RigPilotAgent 已安装并启动。" -ForegroundColor Green
