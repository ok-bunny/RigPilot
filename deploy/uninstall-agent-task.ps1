[CmdletBinding()]
param(
    [string]$ExpectedAgentExePath,
    [string]$ExpectedConfigPath,
    [string]$ExpectedProjectRoot,
    [string]$ExpectedPythonPath,
    [switch]$AllowLegacySourceTask,
    [switch]$AllowLegacyEmptyDescription
)

$ErrorActionPreference = "Stop"

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)) {
    throw "请在管理员 PowerShell 中运行此脚本。"
}

$TaskPath = "\"
$TaskName = "RigPilotAgent"
$KnownDescription = "RigPilot outbound-only device monitoring agent"

$Task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    return
}

if ($Task) {
    if (-not $ExpectedConfigPath -or
        ([bool]$ExpectedAgentExePath -eq [bool]$ExpectedProjectRoot) -or
        ($ExpectedProjectRoot -and (-not $AllowLegacySourceTask -or -not $ExpectedPythonPath)) -or
        ($ExpectedAgentExePath -and ($ExpectedPythonPath -or $AllowLegacySourceTask))) {
        throw "卸载现有任务需要 ExpectedConfigPath 和明确 EXE 路径；源码任务还需 AllowLegacySourceTask、ExpectedProjectRoot、ExpectedPythonPath。"
    }
    $Config = [IO.Path]::GetFullPath($ExpectedConfigPath)
    if ($ExpectedAgentExePath) {
        $Expected = [IO.Path]::GetFullPath($ExpectedAgentExePath)
        $ExpectedArguments = "--config `"$Config`""
        $ExpectedWorkingDirectory = Split-Path -Parent $Expected
    }
    else {
        $Expected = [Environment]::ExpandEnvironmentVariables($ExpectedPythonPath)
        $ExpectedArguments = "-m agent --config `"$Config`""
        $ExpectedWorkingDirectory = [IO.Path]::GetFullPath($ExpectedProjectRoot)
    }
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) {
        throw "A same-name scheduled task has multiple actions; refusing to remove it."
    }
    $Actual = [Environment]::ExpandEnvironmentVariables(
        [string]$Actions[0].Execute
    )
    $Arguments = [string]$Actions[0].Arguments
    $WorkingDirectory = [Environment]::ExpandEnvironmentVariables([string]$Actions[0].WorkingDirectory)
    $HasKnownDescription =
        [string]::Equals(
            [string]$Task.Description,
            $KnownDescription,
            [StringComparison]::Ordinal
        )
    $HasExplicitLegacyDescription = $AllowLegacyEmptyDescription -and
        [string]::IsNullOrWhiteSpace([string]$Task.Description)
    $IsExpectedTask = ($HasKnownDescription -or $HasExplicitLegacyDescription) -and
        [string]::Equals($Actual, $Expected, [StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals($Arguments, $ExpectedArguments, [StringComparison]::Ordinal) -and
        [string]::Equals($WorkingDirectory, $ExpectedWorkingDirectory, [StringComparison]::OrdinalIgnoreCase)
    if (-not $IsExpectedTask) {
        throw "同名计划任务与明确安装上下文不符，拒绝删除：$Actual"
    }
    Stop-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
}

$StopDeadline = [DateTime]::UtcNow.AddSeconds(5)
do {
    $CurrentTask = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $CurrentTask -or $CurrentTask.State -ne "Running") {
        break
    }
    Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $StopDeadline)

try {
    Unregister-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -Confirm:$false `
        -ErrorAction Stop
}
catch {
    $Schtasks = Join-Path $env:SystemRoot "System32\schtasks.exe"
    & $Schtasks /Delete /TN "\$TaskName" /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and
        (Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        throw "Unable to remove the RigPilotAgent scheduled task: $($_.Exception.Message)"
    }
}

if (Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "The RigPilotAgent scheduled task still exists after removal."
}
