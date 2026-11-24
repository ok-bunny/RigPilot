param([string]$Source, [string]$Workspace, [string]$Scenario)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$Tokens = $null
$Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count -gt 0) { throw "Migration script did not parse" }
$Text = [IO.File]::ReadAllText($Source)
$Start = $Text.IndexOf('$StagingRoot = $null')
$End = $Text.IndexOf('function Test-IsAdministrator')
. ([ScriptBlock]::Create($Text.Substring($Start, $End - $Start)))
$Undo = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq "Undo-ProgramOverlay"
}, $true))
if ($Undo.Count -ne 1) { throw "Migration undo function is missing" }
. ([ScriptBlock]::Create($Undo[0].Extent.Text))
foreach ($HelperName in @("Write-MigrationOutcomeLog", "Get-MigrationLogTail")) {
    $Helper = @($Ast.FindAll({ param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $HelperName
    }, $true))
    if ($Helper.Count -ne 1) { throw "Migration helper function is missing: $HelperName" }
    . ([ScriptBlock]::Create($Helper[0].Extent.Text))
}
$Flow = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst]
})[-1]
# Run the real coordinator with fake OS operations. Keep exit from ending the
# harness before its receipt, and never acquire the production global mutex.
$FlowText = $Flow.Extent.Text.Replace('exit 0', '$script:MigrationExitCode = 0').Replace(
    'exit 1', '$script:MigrationExitCode = 1')
$SourceRoot = Join-Path $Workspace "source"
$ProgramDataRoot = $Workspace
$TargetRoot = Join-Path $Workspace "RigPilotAgentPortable"
$PowerShellPath = "fake-powershell"
$script:Events = @()
$script:SourceRunning = $true
$script:TargetRunning = $Scenario -notlike "new-target-*"
$script:TargetTaskPresent = $script:TargetRunning
$script:TargetRecoveryTaskPresent = $script:TargetRunning -or $Scenario -eq "new-target-existing-recovery-install-failed"
[void][IO.Directory]::CreateDirectory($SourceRoot)
[IO.File]::WriteAllText((Join-Path $SourceRoot "identity.txt"), "source-identity")
if ($script:TargetTaskPresent) {
    [void][IO.Directory]::CreateDirectory($TargetRoot)
    foreach ($Name in @("first.txt", "second.txt")) {
        [IO.File]::WriteAllText((Join-Path $TargetRoot $Name), "original-$Name")
    }
}
if ($Scenario -eq "source-is-target") { $SourceRoot = $TargetRoot }
function Test-IsAdministrator { return $true }
function New-Object([string]$TypeName, [object[]]$ArgumentList) {
    if ($TypeName -ne "Threading.Mutex") { throw "Unexpected constructor" }
    $Fake = [pscustomobject]@{}
    $Fake | Add-Member ScriptMethod WaitOne { param($Timeout) return $Scenario -ne "mutex-failure" }
    $Fake | Add-Member ScriptMethod ReleaseMutex { }
    $Fake | Add-Member ScriptMethod Dispose { }
    return $Fake
}
function Exit-MigrationMutex { }
function Assert-PlainPathComponents([string]$Path, [string]$Label) {
    if ($Scenario -eq "bad-source-path" -and $Path -eq $SourceRoot) { throw "Bad source path" }
}
function Assert-PlainPath([string]$Path, [string]$Label) { }
function Assert-PlainTree([string]$Path) { }
function Assert-PortableBundleManifest([string]$Path) {
    if ($Scenario -eq "bad-manifest" -and $Path -eq $SourceRoot) { throw "Bad source manifest" }
}
function Read-EnrollmentIdentity([string]$Path) {
    $Id = if ($Scenario -eq "different-identity" -and $Path -eq $TargetRoot) { "other" } else { "same" }
    return [pscustomobject]@{ AgentId=$Id; AgentSecret="synthetic-secret" }
}
function Test-LiveManagedMiner([string]$Path) { return $false }
function Import-Module([string]$Name, [string]$ErrorAction) {
    if ($Name -ne "ScheduledTasks") { throw "Unexpected module" }
}
function Get-CheckedTaskForRoot([string]$Path, [bool]$Recovery = $false) {
    if ($Recovery) {
        if ($Scenario -eq "target-recovery-task-collision" -and $Path -eq $TargetRoot) {
            throw "Unexpected target recovery task"
        }
        if ($Path -eq $TargetRoot -and $script:TargetRecoveryTaskPresent) {
            return [pscustomobject]@{ TaskName="fake-recovery-task" }
        }
        return
    }
    if ($Scenario -eq "target-task-collision" -and $Path -eq $TargetRoot) {
        throw "Unexpected target task"
    }
    if ($Path -eq $TargetRoot -and $script:TargetTaskPresent) {
        return [pscustomobject]@{ TaskName="fake-target-task" }
    }
}
function Get-AgentProcessesForRoot([string]$Path) {
    if (($Path -eq $SourceRoot -and $script:SourceRunning) -or
        ($Path -eq $TargetRoot -and $script:TargetRunning)) { [pscustomobject]@{ Id=111 } }
}
function Stop-AgentForRoot([string]$Path) {
    $script:Events += "stop:$([IO.Path]::GetFileName($Path))"
    if ($Path -eq $SourceRoot) {
        $script:SourceRunning = $false
        if ($Scenario -eq "stop-source-failed") { throw "Source stop failed" }
    } else {
        $script:TargetRunning = $false
        if ($Scenario -eq "stop-target-failed") { throw "Target stop failed" }
    }
}
function Start-AgentAsAdministrator([string]$Path) {
    $script:Events += "start:$([IO.Path]::GetFileName($Path))"
    if ($Path -eq $SourceRoot) { $script:SourceRunning = $true } else { $script:TargetRunning = $true }
}
function New-RestrictedDirectory([string]$Path) {
    [void][IO.Directory]::CreateDirectory($Path)
    $script:OwnedTemporaryRoots += $Path
}
function Apply-ProgramOverlay([string]$From, [string]$To, [string]$Backup) {
    foreach ($Name in @("first.txt", "second.txt")) {
        $Destination = Join-Path $To $Name
        $BackupFile = Join-Path $Backup $Name
        [IO.File]::Copy($Destination, $BackupFile)
        $script:OverlayRecords += [pscustomobject]@{ Destination=$Destination; Backup=$BackupFile; Existed=$true }
        [IO.File]::WriteAllText($Destination, "changed")
    }
    if ($Scenario -eq "overlay-failed") { throw "Overlay failed" }
}
function Copy-FileAtomically([string]$From, [string]$To) {
    $script:Events += "restore:$([IO.Path]::GetFileName($To))"
    if ($Scenario -eq "rollback-restore-failed") { throw "Restore failed" }
    [IO.File]::Copy($From, $To, $true)
}
function Copy-PlainTree([string]$From, [string]$To, [bool]$ExcludeData) {
    [void][IO.Directory]::CreateDirectory((Join-Path $To "data"))
    [IO.File]::Copy((Join-Path $From "identity.txt"), (Join-Path $To "identity.txt"))
    if ($Scenario -eq "new-target-raced") {
        [void][IO.Directory]::CreateDirectory($TargetRoot)
        [IO.File]::WriteAllText((Join-Path $TargetRoot "identity.txt"), "unrelated-raced-identity")
        $script:TargetRunning = $true
        $script:TargetTaskPresent = $true
    }
}
function Remove-UnmanifestedProgramFiles([string]$Path) { }
function Update-MigratedDataPaths([string]$From, [string]$To) {
    if ($Scenario -eq "new-target-before-install-failed") { throw "Config migration failed" }
}
function Start-Process {
    $script:TargetTaskPresent = $true
    $script:TargetRecoveryTaskPresent = $true
    if ($Scenario -eq "cleanup-failed") {
        $script:TargetRunning = $true
        $Data = Join-Path $TargetRoot "data"
        [void][IO.Directory]::CreateDirectory($Data)
        [IO.File]::WriteAllText((Join-Path $Data "agent-update-ready.json"), "synthetic")
        $Fake = [pscustomobject]@{ ExitCode=0 }
        $Fake | Add-Member ScriptMethod Refresh { }
        return $Fake
    }
    throw "Target install failed after creating its fake task"
}
function Wait-AgentForRoot([string]$Path) {
    if (-not $script:TargetRunning) { throw "Target is not running" }
}
function Unregister-ScheduledTask([string]$TaskName, [string]$TaskPath, [switch]$Confirm) {
    if ($TaskName -eq "fake-recovery-task") {
        $script:Events += "unregister:recovery"
        $script:TargetRecoveryTaskPresent = $false
        return
    }
    if ($TaskName -ne "fake-target-task") { throw "Unexpected task" }
    $script:Events += "unregister:target"
    $script:TargetTaskPresent = $false
}
function Remove-CheckedTree([string]$Path) {
    if (-not $Path.StartsWith($Workspace + [IO.Path]::DirectorySeparatorChar)) { throw "Unsafe test cleanup" }
    if ($Scenario -eq "cleanup-failed" -and $Path -eq $BackupRoot) { throw "Backup cleanup failed" }
    if ([IO.Directory]::Exists($Path)) {
        $script:Events += "remove:$([IO.Path]::GetFileName($Path))"
        [IO.Directory]::Delete($Path, $true)
    }
}
. ([ScriptBlock]::Create($FlowText))
[pscustomobject]@{
    exit_code=$script:MigrationExitCode; events=@($script:Events)
    source_running=$script:SourceRunning; target_running=$script:TargetRunning
    target_task=$script:TargetTaskPresent; target_exists=[IO.Directory]::Exists($TargetRoot)
    target_recovery_task=$script:TargetRecoveryTaskPresent
} | ConvertTo-Json -Depth 4 -Compress
