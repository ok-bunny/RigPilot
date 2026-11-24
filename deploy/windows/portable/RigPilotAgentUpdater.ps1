[CmdletBinding()]
param(
    # The fixed protected installation root.  The updater only ever operates
    # on this one ProgramData folder.
    [Parameter(Mandatory = $true)][string]$PortableRoot,

    # Activate mode (protocol v1).  Every in-service Agent since 0.8.48
    # invokes a staged copy of this script with exactly these parameters and
    # no -Action value, so they stay optional here and are validated below.
    [string]$StagedBundle,
    [string]$Version,
    [string]$ActivationId,
    [string]$AgentId,
    [string]$CommandId,
    [int]$AgentPid = 0,
    [int]$LauncherPid = 0,
    [string]$ReadyPath,

    # Recover mode (P3, user-approved 2026-11-03): the bounded anchor-level
    # recovery role symmetric with the Linux updater's ``recover``
    # subcommand.  It reads and writes only protocol-v1 receipts, markers,
    # and pointer files, so it serves every in-service 0.8.48+ Agent without
    # requiring an Agent upgrade first.
    [ValidateSet("Activate", "Recover")][string]$Action = "Activate"
)

$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$PortableRoot = [IO.Path]::GetFullPath($PortableRoot).TrimEnd('\')
$DataRoot = Join-Path $PortableRoot "data"
$PointerPath = Join-Path $DataRoot "active-agent.json"
$LauncherPath = Join-Path $PortableRoot "portable-agent.ps1"
$VersionsRoot = Join-Path $PortableRoot "versions"
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
# The recovery deadline is anchored on the pointer's own durable mtime:
# confirmation window (75s) plus the same bounded grace the Agent-side
# recovery uses, so updater-level and Agent-level recovery agree on one
# bounded window.  P4 treats a wall clock provably behind that anchor as
# expired so no-RTC devices never wait for NTP.
$RecoveryGraceSeconds = 195
$RecoveryClockResetToleranceTicks = 50000000L
$RecoveryRestartAttemptLimit = 5
$PointerActivated = $false
$NewLauncherProcess = $null
$ActivationConfirmed = $false
$HandoffStarted = $false
$OwnedSlotPath = $null

function Write-AtomicJson([string]$Path, [object]$Value) {
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $Parent -Force | Out-Null
    $Temporary = Join-Path $Parent ("." + [IO.Path]::GetFileName($Path) + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        $Json = $Value | ConvertTo-Json -Depth 8 -Compress
        [IO.File]::WriteAllText($Temporary, $Json + [Environment]::NewLine, $Utf8NoBom)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $DiscardBackup = Join-Path $Parent (".replace." + [Guid]::NewGuid().ToString("N") + ".tmp")
            try {
                [IO.File]::Replace($Temporary, $Path, $DiscardBackup, $true)
            }
            finally {
                if (Test-Path -LiteralPath $DiscardBackup) {
                    Remove-Item -LiteralPath $DiscardBackup -Force
                }
            }
        }
        else {
            [IO.File]::Move($Temporary, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $Temporary) {
            Remove-Item -LiteralPath $Temporary -Force
        }
    }
}

function Assert-PlainDirectory([string]$Path, [string]$Label) {
    $Item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $Item.PSIsContainer -or
        ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label is not a safe directory."
    }
}

function Assert-PlainDirectoryChain([string]$Path, [string]$Boundary, [string]$Label) {
    $FullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $FullBoundary = [IO.Path]::GetFullPath($Boundary).TrimEnd('\')
    $BoundaryPrefix = $FullBoundary + '\'
    if (
        -not [string]::Equals($FullPath, $FullBoundary, [StringComparison]::OrdinalIgnoreCase) -and
        -not $FullPath.StartsWith($BoundaryPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label escapes its trusted boundary."
    }
    $Current = $FullPath
    while ($true) {
        Assert-PlainDirectory $Current $Label
        if ([string]::Equals($Current, $FullBoundary, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $Parent = [IO.Directory]::GetParent($Current)
        if ($null -eq $Parent) { throw "$Label has no trusted parent." }
        $Current = $Parent.FullName.TrimEnd('\')
    }
}

function Wait-ProcessExit([int]$Id, [int]$Seconds) {
    if ($Id -le 0 -or $Id -eq $PID) { return }
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $Deadline) {
        if ($null -eq (Get-Process -Id $Id -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out waiting for process $Id to exit."
}

function Test-ConfirmationReceipt {
    # P2 (user-approved 2026-11-03): the exact confirmation predicate, shared
    # by the activation wait loop, its post-deadline recheck, and bounded
    # recovery.  Returns the validated receipt object or $false.
    param(
        [string]$Path,
        [string]$ExpectedVersion,
        [string]$ExpectedActivationId,
        [string]$ExpectedCommandId,
        [string]$ExpectedAgentId
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $Item.Length -gt 32768) {
        return $false
    }
    try {
        $Receipt = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        return $false
    }
    $Heartbeats = $Receipt.health_heartbeats
    if ([string]$Receipt.activation_id -ne $ExpectedActivationId -or
        [string]$Receipt.version -ne $ExpectedVersion -or
        [string]$Receipt.command_id -ne $ExpectedCommandId -or
        ($Heartbeats -is [bool]) -or
        [int]$Heartbeats -lt 2) {
        return $false
    }
    if (-not [string]::IsNullOrEmpty($ExpectedAgentId) -and
        [string]$Receipt.agent_id -ne $ExpectedAgentId) {
        return $false
    }
    return $Receipt
}

function Test-RecoveryDeadlineExpired {
    # P4 (user-approved 2026-11-03): the recovery window is measured from the
    # durable mtime of the pointer this update itself switched, never from a
    # wall-clock deadline alone.  After a no-RTC power loss the system clock
    # restarts behind that timestamp; the elapsed computation would then go
    # negative and the deadline could only expire after NTP.  A clock that is
    # provably behind the durable anchor counts as expired.
    param([long]$AnchorUtcFileTime, [int]$GraceSeconds)
    $NowFileTime = [DateTime]::UtcNow.ToFileTime()
    if ($NowFileTime -lt ($AnchorUtcFileTime - $RecoveryClockResetToleranceTicks)) {
        return $true
    }
    return (($NowFileTime - $AnchorUtcFileTime) -ge ([int64]$GraceSeconds * 10000000L))
}

function Complete-ConfirmedActivation {
    # Shared by the wait loop and its post-deadline recheck: keep a confirmed
    # replacement online; the local success receipt is best-effort.
    $script:ActivationConfirmed = $true
    $BoundIntentPath = Join-Path (Split-Path -Parent $StatusPath) "recovery-restart.json"
    try {
        $BoundIntent = Get-PlainJsonFile $BoundIntentPath
        if ($null -ne $BoundIntent -and [string]$BoundIntent.intent_kind -eq "bound-rollback-restart.v1" -and
            [string]$BoundIntent.activation_id -ceq $ActivationId) {
            Remove-Item -LiteralPath $BoundIntentPath -Force
        }
    }
    catch { }
    try {
        Write-AtomicJson $StatusPath ([ordered]@{
            schema_version = 1
            status = "succeeded"
            version = $Version
            activation_id = $ActivationId
            command_id = $CommandId
            agent_id = $AgentId
            stable_launcher_retained = $true
            stable_updater_retained = $true
        })
    }
    catch {
        # Local diagnostics are secondary to keeping the confirmed
        # replacement online.
    }
    return $true
}

function Assert-StagedManifest {
    $ManifestPath = Join-Path $StagedBundle "agent-release.json"
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "The staged release manifest is missing."
    }
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Names = @($Manifest.PSObject.Properties.Name | Sort-Object)
    $ExpectedNames = @("architecture", "files", "platform", "schema_version", "version")
    if ((Compare-Object $Names $ExpectedNames).Count -ne 0 -or
        $Manifest.schema_version -is [bool] -or
        [int]$Manifest.schema_version -ne 1 -or
        [string]$Manifest.version -ne $Version -or
        [string]$Manifest.platform -ne "windows" -or
        [string]$Manifest.architecture -ne "x86_64") {
        throw "The staged release manifest does not match this activation."
    }
    $Declared = @{}
    foreach ($Property in @($Manifest.files.PSObject.Properties)) {
        $Relative = [string]$Property.Name
        $Digest = ([string]$Property.Value).ToLowerInvariant()
        $First = $Relative.Split('/')[0].ToLowerInvariant()
        if ([string]::IsNullOrWhiteSpace($Relative) -or
            $First -in @("data", "versions") -or
            [IO.Path]::IsPathRooted($Relative) -or
            $Relative.Contains("\") -or
            $Relative.Contains(":") -or
            $Relative -match '(^|/)\.\.(/|$)' -or
            $Digest -notmatch '^[0-9a-f]{64}$') {
            throw "The staged release manifest contains an unsafe file entry."
        }
        $Candidate = [IO.Path]::GetFullPath((Join-Path $StagedBundle ($Relative.Replace('/', '\'))))
        if (-not $Candidate.StartsWith($StagedBundle + '\', [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            throw "A declared staged release file is missing."
        }
        $Item = Get-Item -LiteralPath $Candidate -Force
        if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "A staged release file is a reparse point."
        }
        $Actual = (Get-FileHash -LiteralPath $Candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Actual -ne $Digest) {
            throw "A staged release file failed SHA-256 verification."
        }
        $Declared[$Relative.ToLowerInvariant()] = $true
    }
    foreach ($Required in @(
        "rigpilotagent.exe",
        "rigpilotagentupdater.ps1",
        "portable-agent.ps1",
        "start-rigpilotagent.bat"
    )) {
        if (-not $Declared.ContainsKey($Required)) {
            throw "The staged release is missing $Required."
        }
    }
    $ActualFiles = @(Get-ChildItem -LiteralPath $StagedBundle -Recurse -File -Force | ForEach-Object {
        $_.FullName.Substring($StagedBundle.Length + 1).Replace('\', '/').ToLowerInvariant()
    } | Where-Object { $_ -ne "agent-release.json" })
    if ($ActualFiles.Count -ne $Declared.Count) {
        throw "The staged release contains undeclared files."
    }
    foreach ($ActualFile in $ActualFiles) {
        if (-not $Declared.ContainsKey($ActualFile)) {
            throw "The staged release contains an undeclared file."
        }
    }
}

function Restore-FileAtomically([string]$Source, [string]$Destination) {
    $Parent = Split-Path -Parent $Destination
    $Temporary = Join-Path $Parent (".rollback." + [Guid]::NewGuid().ToString("N") + ".tmp")
    [IO.File]::Copy($Source, $Temporary, $true)
    try {
        if (Test-Path -LiteralPath $Destination -PathType Leaf) {
            $Discard = Join-Path $Parent (".rollback-backup." + [Guid]::NewGuid().ToString("N") + ".tmp")
            try {
                [IO.File]::Replace($Temporary, $Destination, $Discard, $true)
            }
            finally {
                if (Test-Path -LiteralPath $Discard) {
                    Remove-Item -LiteralPath $Discard -Force
                }
            }
        }
        else {
            [IO.File]::Move($Temporary, $Destination)
        }
    }
    finally {
        if (Test-Path -LiteralPath $Temporary) {
            Remove-Item -LiteralPath $Temporary -Force
        }
    }
}

function Start-ActiveAgent([string]$RecoveryActivationId = "") {
    $Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
        $LauncherPath + '" -Action Start -Scheduled'
    # Transient context only: the retained launch mutex can serialize a
    # recovery child against an explicit Stop without adding a startup task.
    $Before = [Environment]::GetEnvironmentVariable("RIGPILOT_UPDATE_RECOVERY_ID", "Process")
    try {
        [Environment]::SetEnvironmentVariable("RIGPILOT_UPDATE_RECOVERY_ID", $RecoveryActivationId, "Process")
        return Start-Process -FilePath $PowerShellPath -ArgumentList $Arguments `
            -WorkingDirectory $PortableRoot -WindowStyle Hidden -PassThru
    }
    finally {
        [Environment]::SetEnvironmentVariable("RIGPILOT_UPDATE_RECOVERY_ID", $Before, "Process")
    }
}

function Get-RecoveryIdentityDigest([string]$RootData) {
    $State = Get-PlainJsonFile (Join-Path $RootData "state.json") -MaxBytes 67108864
    if ($null -eq $State -or [string]$State.agent_id -notmatch '^[0-9a-f-]{36}$' -or
        [string]$State.agent_secret -notmatch '^rpa_[A-Za-z0-9_-]{48}$') {
        throw "Recovery requires the existing enrolled identity."
    }
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { $Hash = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$State.agent_secret)) }
    finally { $Hasher.Dispose() }
    return @{ agent_id = [string]$State.agent_id; digest = (-join ($Hash | ForEach-Object { $_.ToString("x2") })) }
}

function Test-SameRecoveryPointer($Left, $Right) {
    if ($null -eq $Left -or $null -eq $Right) { return $null -eq $Left -and $null -eq $Right }
    $Names = @("activation_id", "command_id", "relative_executable", "schema_version", "version")
    if ((Compare-Object @($Left.PSObject.Properties.Name | Sort-Object) $Names).Count -ne 0 -or
        (Compare-Object @($Right.PSObject.Properties.Name | Sort-Object) $Names).Count -ne 0) { return $false }
    foreach ($Name in $Names) { if ([string]$Left.$Name -cne [string]$Right.$Name) { return $false } }
    return $true
}

function Enter-RecoveryIntentMutex {
    $Mutex = New-Object Threading.Mutex($false, ("Global\RigPilotAgentPortableLaunch-" + (Get-PortableUpdaterIdentity)))
    try {
        try { $Acquired = $Mutex.WaitOne([TimeSpan]::FromSeconds(15)) }
        catch [Threading.AbandonedMutexException] { $Acquired = $true }
        if (-not $Acquired) { throw "Portable startup/stop is busy." }
        return $Mutex
    }
    catch { $Mutex.Dispose(); throw }
}

function Exit-RecoveryIntentMutex($Mutex) {
    try { $Mutex.ReleaseMutex() } finally { $Mutex.Dispose() }
}

function New-BoundRecoveryIntent {
    # Save the verified old target BEFORE readiness lets the old Agent exit.
    # This extends the existing per-job restart marker, never the v1 wire
    # ready/confirmation schema and never the installed updater executable.
    $IntentMutex = Enter-RecoveryIntentMutex
    try {
    $Identity = Get-RecoveryIdentityDigest $DataRoot
    if ($Identity.agent_id -cne $AgentId) { throw "Recovery identity differs from activation." }
    $Previous = Get-PlainJsonFile $PointerPath
    $Relative = "RigPilotAgent.exe"
    if ($null -ne $Previous) {
        $PriorVersion = [string]$Previous.version
        if ((Compare-Object @($Previous.PSObject.Properties.Name | Sort-Object) `
                @("activation_id", "command_id", "relative_executable", "schema_version", "version")).Count -ne 0 -or
            [int]$Previous.schema_version -ne 1 -or $PriorVersion -notmatch '^\d+\.\d+\.\d+$' -or
            [string]$Previous.relative_executable -cne "versions/$PriorVersion/RigPilotAgent.exe" -or
            [string]$Previous.activation_id -notmatch '^[0-9a-f]{32}$' -or
            [string]$Previous.command_id -notmatch '^[0-9a-f-]{36}$') { throw "Previous pointer is invalid." }
        $Relative = [string]$Previous.relative_executable
    }
    elseif (Test-Path -LiteralPath $PointerPath) { throw "Previous pointer is not a regular file." }
    $Executable = Join-Path $PortableRoot ($Relative.Replace('/', '\'))
    Assert-PlainDirectoryChain (Split-Path -Parent $Executable) $PortableRoot "Previous program"
    $File = Get-Item -LiteralPath $Executable -Force
    if ($File.PSIsContainer -or ($File.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Previous executable is unsafe."
    }
    $Manifest = Get-PlainJsonFile (Join-Path (Split-Path -Parent $Executable) "agent-release.json") -MaxBytes 2097152
    $RestoreVersion = [string]$Manifest.version
    $Hash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($null -eq $Manifest -or $RestoreVersion -notmatch '^\d+\.\d+\.\d+$' -or
        [string]$Manifest.platform -ne "windows" -or [string]$Manifest.architecture -ne "x86_64" -or
        [string]$Manifest.files.'RigPilotAgent.exe' -cne $Hash -or
        ([Version]$RestoreVersion) -ge ([Version]$Version) -or
        ($null -ne $Previous -and [string]$Previous.version -cne $RestoreVersion)) {
        throw "Previous program is not an exact older verified release."
    }
    $Process = Get-Process -Id $AgentPid -ErrorAction Stop
    if (-not [string]::Equals([IO.Path]::GetFullPath([string]$Process.Path),
            [IO.Path]::GetFullPath($Executable), [StringComparison]::OrdinalIgnoreCase)) {
        throw "Old Agent PID does not own the verified previous executable."
    }
    $UpdaterProcess = Get-Process -Id $PID -ErrorAction Stop
    $CandidateHash = (Get-FileHash -LiteralPath (Join-Path $StagedBundle "RigPilotAgent.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
    $Intent = [ordered]@{
        schema_version = 1; intent_kind = "bound-rollback-restart.v1"
        activation_id = $ActivationId; command_id = $CommandId; agent_id = $AgentId
        identity_sha256 = $Identity.digest; version = $Version
        restore_version = $RestoreVersion; restore_relative = $Relative; restore_sha256 = $Hash
        restore_pointer = $Previous; source_pid = $AgentPid
        source_started_at = $Process.StartTime.ToUniversalTime().ToString("o")
        updater_pid = $PID; updater_started_at = $UpdaterProcess.StartTime.ToUniversalTime().ToString("o")
        candidate_sha256 = $CandidateHash
        phase = "handoff"; attempts = 0; created_ticks = [DateTime]::UtcNow.ToFileTime()
    }
    Write-AtomicJson (Join-Path (Split-Path -Parent $StagedBundle) "recovery-restart.json") $Intent
    }
    finally { Exit-RecoveryIntentMutex $IntentMutex }
}

function Invoke-BoundRecoveryRestart([string]$Root, [string]$JobPath) {
    $MarkerPath = Join-Path $JobPath "recovery-restart.json"
    $Intent = Get-PlainJsonFile $MarkerPath
    if ($null -eq $Intent -or [string]$Intent.intent_kind -ne "bound-rollback-restart.v1") { return }
    if ([string]$Intent.phase -in @("cancelled", "abandoned")) { return }
    $RootData = Join-Path $Root "data"
    $Identity = Get-RecoveryIdentityDigest $RootData
    $Id = [string]$Intent.activation_id
    $OldVersion = [string]$Intent.restore_version
    $Relative = [string]$Intent.restore_relative
    if ([int]$Intent.schema_version -ne 1 -or $Id -notmatch '^[0-9a-f]{32}$' -or
        (Split-Path -Leaf $JobPath) -cne $Id -or [string]$Intent.command_id -notmatch '^[0-9a-f-]{36}$' -or
        [string]$Intent.agent_id -cne $Identity.agent_id -or [string]$Intent.identity_sha256 -cne $Identity.digest -or
        $OldVersion -notmatch '^\d+\.\d+\.\d+$' -or [string]$Intent.version -notmatch '^\d+\.\d+\.\d+$' -or
        ([Version]$OldVersion) -ge ([Version]([string]$Intent.version)) -or
        $Relative -cnotin @("RigPilotAgent.exe", "versions/$OldVersion/RigPilotAgent.exe") -or
        [string]$Intent.restore_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Intent.phase -notin @("handoff", "rollback") -or
        $Intent.attempts -is [bool] -or [int]$Intent.attempts -lt 0 -or
        [long]$Intent.created_ticks -le 0 -or [int]$Intent.source_pid -le 0) { return }
    $CurrentPointerPath = Join-Path $RootData "active-agent.json"
    $Current = Get-PlainJsonFile $CurrentPointerPath
    if ($null -eq $Current -and (Test-Path -LiteralPath $CurrentPointerPath)) { return }
    $Restored = Test-SameRecoveryPointer $Current $Intent.restore_pointer
    if (($Relative -eq "RigPilotAgent.exe") -ne ($null -eq $Intent.restore_pointer)) { return }
    if ($null -ne $Intent.restore_pointer -and
        ([string]$Intent.restore_pointer.relative_executable -cne $Relative -or
         [string]$Intent.restore_pointer.version -cne $OldVersion)) { return }
    $ExpectedPending = [pscustomobject]@{
        schema_version = 1; activation_id = $Id; command_id = [string]$Intent.command_id
        version = [string]$Intent.version; relative_executable = "versions/$($Intent.version)/RigPilotAgent.exe"
    }
    $Pending = $null -ne $Current -and (Test-SameRecoveryPointer $Current $ExpectedPending)
    if (-not $Restored -and -not $Pending) { return }
    $Executable = Join-Path $Root ($Relative.Replace('/', '\'))
    Assert-PlainDirectoryChain (Split-Path -Parent $Executable) $Root "Recovery previous program"
    $File = Get-Item -LiteralPath $Executable -Force
    if ($File.PSIsContainer -or ($File.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$Intent.restore_sha256) { return }
    # Exact old process still alive means the handoff never completed. Do not
    # launch a second instance or reinterpret a normal no-transaction stop.
    $Original = Get-Process -Id ([int]$Intent.source_pid) -ErrorAction SilentlyContinue
    $OriginalAlive = $false
    if ($null -ne $Original) {
        $OriginalAlive = [string]::Equals([IO.Path]::GetFullPath([string]$Original.Path),
            [IO.Path]::GetFullPath($Executable), [StringComparison]::OrdinalIgnoreCase) -and
            $Original.StartTime.ToUniversalTime().ToString("o") -ceq [string]$Intent.source_started_at
    }
    if ($OriginalAlive -or (Test-PortableAgentRunning $RootData $Executable)) {
        if ($Restored -and [string]$Intent.phase -eq "rollback") { Remove-Item -LiteralPath $MarkerPath -Force }
        elseif ($Restored -and [string]$Intent.phase -eq "handoff" -and
            (Test-RecoveryDeadlineExpired -AnchorUtcFileTime ([long]$Intent.created_ticks) -GraceSeconds $RecoveryGraceSeconds)) {
            # A dead helper which never handed off must not reserve permission
            # to restart an otherwise healthy old Agent months later.
            $UpdaterGone = $false
            try {
                $Owner = Get-Process -Id ([int]$Intent.updater_pid) -ErrorAction Stop
                $UpdaterGone = $null -eq $Owner -or
                    $Owner.StartTime.ToUniversalTime().ToString("o") -cne [string]$Intent.updater_started_at
            }
            catch { $UpdaterGone = $_.CategoryInfo.Category -eq "ObjectNotFound" }
            if ($UpdaterGone) {
                $IntentMutex = Enter-RecoveryIntentMutex
                try {
                    $Latest = Get-PlainJsonFile $MarkerPath
                    if ($null -ne $Latest -and [string]$Latest.phase -eq "handoff") {
                        $Latest.phase = "abandoned"
                        Write-AtomicJson $MarkerPath $Latest
                    }
                }
                finally { Exit-RecoveryIntentMutex $IntentMutex }
            }
        }
        return
    }
    $Confirmation = Test-ConfirmationReceipt -Path (Join-Path $RootData "agent-updates\confirmations\$Id.json") `
        -ExpectedVersion ([string]$Intent.version) -ExpectedActivationId $Id `
        -ExpectedCommandId ([string]$Intent.command_id) -ExpectedAgentId ([string]$Intent.agent_id)
    if ($null -ne $Confirmation -and $Confirmation -isnot [bool]) {
        Remove-Item -LiteralPath $MarkerPath -Force
        return
    }
    if ([string]$Intent.phase -eq "handoff" -and -not (Test-RecoveryDeadlineExpired `
            -AnchorUtcFileTime ([long]$Intent.created_ticks) -GraceSeconds $RecoveryGraceSeconds)) { return }
    if ([int]$Intent.attempts -ge $RecoveryRestartAttemptLimit) { return }
    $IntentMutex = Enter-RecoveryIntentMutex
    try {
    # Stop/RemoveAutostart uses this same startup mutex. Reread after taking
    # it so an earlier cancellation cannot be overwritten by a stale copy.
    $Latest = Get-PlainJsonFile $MarkerPath
    if ($null -eq $Latest -or [string]$Latest.phase -notin @("handoff", "rollback") -or
        [string]$Latest.activation_id -cne $Id -or [int]$Latest.attempts -ge $RecoveryRestartAttemptLimit -or
        -not (Test-SameRecoveryPointer (Get-PlainJsonFile $CurrentPointerPath) $Current)) { return }
    # Confirmation can arrive while the startup mutex is contended. Recheck
    # at the mutation boundary before stopping a candidate or restoring data.
    $ConfirmedAfterLock = Test-ConfirmationReceipt -Path (Join-Path $RootData "agent-updates\confirmations\$Id.json") `
        -ExpectedVersion ([string]$Latest.version) -ExpectedActivationId $Id `
        -ExpectedCommandId ([string]$Latest.command_id) -ExpectedAgentId ([string]$Latest.agent_id)
    if ($null -ne $ConfirmedAfterLock -and $ConfirmedAfterLock -isnot [bool]) {
        Remove-Item -LiteralPath $MarkerPath -Force
        return
    }
    $Intent = $Latest
    $Intent.phase = "rollback"
    $Intent.attempts = [int]$Intent.attempts + 1
    Write-AtomicJson $MarkerPath $Intent
    if ($Pending) {
        Stop-NewAgentIfOwned -TargetDataRoot $RootData -TargetSlotPath (Join-Path $Root "versions\$($Intent.version)")
        if (Test-PortableAgentRunning $RootData (Join-Path $Root "versions\$($Intent.version)\RigPilotAgent.exe")) { return }
        if ($null -eq $Intent.restore_pointer) {
            Remove-Item -LiteralPath $CurrentPointerPath -Force
        }
        else { Write-AtomicJson $CurrentPointerPath $Intent.restore_pointer }
    }
    Write-AtomicJson (Join-Path $JobPath "activation-result.json") ([ordered]@{
        schema_version = 1; status = "rolled_back"; version = [string]$Intent.version
        activation_id = $Id; command_id = [string]$Intent.command_id; agent_id = [string]$Intent.agent_id
        error = "verified update handoff failed; bounded previous-Agent restart requested"
    })
    }
    finally { Exit-RecoveryIntentMutex $IntentMutex }
    # Marker stays until a later tick proves the old process actually ran.
    # A created PowerShell launcher alone is never recovery success.
    Start-ActiveAgent -RecoveryActivationId $Id | Out-Null
}

function Stop-NewAgentIfOwned {
    # A malformed PID record is never used to terminate an unknown process.
    param(
        [string]$TargetDataRoot = $DataRoot,
        [string]$TargetSlotPath = $SlotPath
    )
    $PidPath = Join-Path $TargetDataRoot "agent.pid.json"
    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) { return }
    try {
        $Record = Get-Content -LiteralPath $PidPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $Process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop
        $Expected = [IO.Path]::GetFullPath((Join-Path $TargetSlotPath "RigPilotAgent.exe"))
        $Recorded = [IO.Path]::GetFullPath([string]$Record.executable_path)
        $Actual = $null
        try { $Actual = [IO.Path]::GetFullPath([string]$Process.Path) } catch {}
        if ([string]::IsNullOrWhiteSpace($Actual)) {
            try { $Actual = [IO.Path]::GetFullPath([string]$Process.MainModule.FileName) } catch {}
        }
        $RecordedStart = [DateTime]::Parse([string]$Record.started_at).ToUniversalTime()
        $ActualStart = $Process.StartTime.ToUniversalTime()
        if ([string]::Equals($Expected, $Recorded, [StringComparison]::OrdinalIgnoreCase) -and
            [string]::Equals($Expected, $Actual, [StringComparison]::OrdinalIgnoreCase) -and
            [Math]::Abs(($ActualStart - $RecordedStart).TotalSeconds) -lt 1) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    catch {
        # A malformed PID record is never used to terminate an unknown process.
    }
}

function Get-PlainJsonFile {
    # Returns the parsed plain regular file or $null (missing) and throws on
    # unsafe shapes.  Recovery never follows a reparse point.
    param([string]$Path, [int]$MaxBytes = 32768)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $Item.Length -gt $MaxBytes) {
        throw "A recovery metadata file is unsafe: $Path"
    }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Remove-StagedPayload {
    # Delete the bulky, never-executable staging payload while keeping every
    # receipt and marker the reporting path still needs.
    param([string]$JobPath)
    if (-not (Test-Path -LiteralPath $JobPath -PathType Container)) { return }
    Assert-PlainDirectory $JobPath "Staged activation job"
    foreach ($Name in @(
        "release.zip",
        "updater.log",
        "updater-ready.json",
        "active-agent.previous.json",
        "RigPilotAgentUpdater.ps1"
    )) {
        $Path = Join-Path $JobPath $Name
        if (-not (Test-Path -LiteralPath $Path)) { continue }
        $Item = Get-Item -LiteralPath $Path -Force
        if (-not $Item.PSIsContainer -and
            ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) {
            Remove-Item -LiteralPath $Path -Force
        }
    }
    $BundlePath = Join-Path $JobPath "bundle"
    if (Test-Path -LiteralPath $BundlePath -PathType Container) {
        Assert-PlainDirectory $BundlePath "Staged release bundle"
        Remove-Item -LiteralPath $BundlePath -Recurse -Force
    }
}

function Remove-AbandonedUpdateSlots {
    # Reclaim slots a concluded update left behind: the ``agent-updates\
    # quarantine`` entries a rollback preserved as evidence and any
    # ``.incoming-*`` staging slot a hard-crashed activation abandoned.
    # Without this, repeated failures permanently consumed roughly twice the
    # package size each.  Best-effort hygiene only: failures are ignored so
    # they can never gate an otherwise healthy activation or recovery.
    param([string]$Root)
    $VersionsRoot = Join-Path $Root "versions"
    if (Test-Path -LiteralPath $VersionsRoot -PathType Container) {
        foreach ($Entry in @(Get-ChildItem -LiteralPath $VersionsRoot -Directory -Force -ErrorAction SilentlyContinue)) {
            if (($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if ($Entry.Name -notmatch '^\.incoming-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-[0-9a-f]{32}$') { continue }
            Remove-Item -LiteralPath $Entry.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $QuarantineRoot = Join-Path $Root "data\agent-updates\quarantine"
    if (Test-Path -LiteralPath $QuarantineRoot -PathType Container) {
        foreach ($Entry in @(Get-ChildItem -LiteralPath $QuarantineRoot -Directory -Force -ErrorAction SilentlyContinue)) {
            if (($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if ($Entry.Name -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-[0-9a-f]{32}$') { continue }
            Remove-Item -LiteralPath $Entry.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-PortableAgentRunning {
    # True when the PID record selects a live process running the pointer's
    # own executable.  Malformed records read as "not running" so recovery
    # stays bounded instead of failing closed on unrelated damage.
    param([string]$TargetDataRoot, [string]$ExpectedExecutable)
    try {
        $Record = Get-PlainJsonFile (Join-Path $TargetDataRoot "agent.pid.json")
        if ($null -eq $Record) { return $false }
        $Process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop
        $Actual = $null
        try { $Actual = [IO.Path]::GetFullPath([string]$Process.Path) } catch {}
        if ([string]::IsNullOrWhiteSpace($Actual)) {
            try { $Actual = [IO.Path]::GetFullPath([string]$Process.MainModule.FileName) } catch {}
        }
        return [string]::Equals(
            $Actual, [IO.Path]::GetFullPath($ExpectedExecutable),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Complete-PendingRecoveryRestart {
    # Phase A of bounded recovery: a rollback whose launcher restart failed
    # leaves a durable marker in the activation job.  Retry the restart a
    # bounded number of times across task ticks, then stop.
    param([string]$Root, [string]$TargetDataRoot, [string]$StagedRoot)
    if (-not (Test-Path -LiteralPath $StagedRoot -PathType Container)) { return }
    $CurrentPointerPath = Join-Path $TargetDataRoot "active-agent.json"
    # Stream immediate children: completed/empty historical jobs neither
    # consume an action quota nor prevent a later valid transaction being read.
    Assert-PlainDirectory $StagedRoot "Recovery jobs root"
    Get-ChildItem -LiteralPath $StagedRoot -Directory -Force | ForEach-Object {
        $Job = $_
        if ($Job.Name -notmatch '^[0-9a-f]{32}$' -or
            ($Job.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return }
        $MarkerPath = Join-Path $Job.FullName "recovery-restart.json"
        if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) { return }
        try {
            $Marker = Get-PlainJsonFile $MarkerPath
            if ($null -eq $Marker -or [string]$Marker.phase -in @("cancelled", "abandoned")) { return }
            if ([string]$Marker.intent_kind -eq "bound-rollback-restart.v1") {
                Assert-PlainDirectoryChain $Job.FullName $TargetDataRoot "Recovery job"
                Invoke-BoundRecoveryRestart -Root $Root -JobPath $Job.FullName
                return
            }
            $Pointer = Get-PlainJsonFile $CurrentPointerPath
            if ($null -eq $Pointer) { return }
            $Attempts = 0
            if ($Marker.attempts -isnot [bool]) {
                $Attempts = [int]$Marker.attempts
            }
            $RestoreRelative = [string]$Marker.restore_relative
            $RestoreExecutable = Join-Path $Root ($RestoreRelative.Replace('/', '\'))
            if ([string]$Pointer.relative_executable -ne $RestoreRelative) {
                # The restore is no longer current (operator or newer update);
                # the marker is obsolete.
                Remove-Item -LiteralPath $MarkerPath -Force
                return
            }
            if (Test-PortableAgentRunning -TargetDataRoot $TargetDataRoot -ExpectedExecutable $RestoreExecutable) {
                Remove-Item -LiteralPath $MarkerPath -Force
                return
            }
            if ($Attempts -ge $RecoveryRestartAttemptLimit) { return }
            Write-AtomicJson $MarkerPath ([ordered]@{
                schema_version = 1
                activation_id = [string]$Marker.activation_id
                version = [string]$Marker.version
                restore_version = [string]$Marker.restore_version
                restore_relative = $RestoreRelative
                attempts = ($Attempts + 1)
            })
            try {
                Start-ActiveAgent | Out-Null
            }
            catch {
                # The attempt is already counted durably; the next tick retries.
            }
        }
        catch {
            # A malformed marker never blocks the pointer evaluation below.
        }
    }
}

function Invoke-PortableRecovery {
    # P3 (user-approved 2026-11-03): the bounded recovery role symmetric with
    # the Linux updater's ``recover`` subcommand.  Protocol v1 only: every
    # file read or written already exists in the 0.8.48+ on-device layout.
    # Returns the process exit code.
    param([string]$Root)

    $TargetDataRoot = Join-Path $Root "data"
    $StagedRoot = Join-Path $TargetDataRoot "agent-updates\staged"

    Complete-PendingRecoveryRestart -Root $Root -TargetDataRoot $TargetDataRoot `
        -StagedRoot $StagedRoot

    if (-not (Test-Path -LiteralPath $PointerPath -PathType Leaf)) { return 0 }
    $PointerItem = Get-Item -LiteralPath $PointerPath -Force -ErrorAction Stop
    if (($PointerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $PointerItem.Length -gt 32768) {
        return 0
    }
    $Pointer = Get-Content -LiteralPath $PointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Names = @($Pointer.PSObject.Properties.Name | Sort-Object)
    $ExpectedNames = @("activation_id", "command_id", "relative_executable", "schema_version", "version")
    $ActiveVersion = [string]$Pointer.version
    $ActiveActivationId = [string]$Pointer.activation_id
    $ActiveCommandId = [string]$Pointer.command_id
    if ((Compare-Object $Names $ExpectedNames).Count -ne 0 -or
        [int]$Pointer.schema_version -ne 1 -or
        $ActiveVersion -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
        $ActiveActivationId -notmatch '^[0-9a-f]{32}$' -or
        $ActiveCommandId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' -or
        [string]$Pointer.relative_executable -ne "versions/$ActiveVersion/RigPilotAgent.exe") {
        # Fail closed on an unknown pointer: recovery never mutates a layout
        # it cannot fully validate.  The launcher's elevated repair path owns
        # broken pointers instead.
        return 0
    }
    # Bound intents (including operator cancellation) have their own exact
    # old-target/identity checks. Never bypass them via the legacy fallback.
    $BoundIntent = Get-PlainJsonFile (Join-Path $StagedRoot "$ActiveActivationId\recovery-restart.json")
    if ($null -ne $BoundIntent -and [string]$BoundIntent.intent_kind -eq "bound-rollback-restart.v1") { return 0 }

    # Scheduled Recover has no activation command-line parameters. Select the
    # receipt from the validated active pointer, never an Activate-mode global.
    $ActiveConfirmationPath = Join-Path $TargetDataRoot (
        "agent-updates\confirmations\$ActiveActivationId.json"
    )
    $Confirmation = Test-ConfirmationReceipt -Path $ActiveConfirmationPath `
        -ExpectedVersion $ActiveVersion -ExpectedActivationId $ActiveActivationId `
        -ExpectedCommandId $ActiveCommandId -ExpectedAgentId ""
    if ($null -ne $Confirmation -and $Confirmation -isnot [bool]) {
        # The replacement proved two authenticated heartbeats.  Finalize the
        # receipt the updater may have died before writing, then free the
        # staged payload once the controller report is durable.
        $JobPath = Join-Path $StagedRoot $ActiveActivationId
        if (Test-Path -LiteralPath $JobPath -PathType Container) {
            $ResultPath = Join-Path $JobPath "activation-result.json"
            $ConfirmAgentId = [string]$Confirmation.agent_id
            if (-not (Test-Path -LiteralPath $ResultPath -PathType Leaf) -and
                $ConfirmAgentId -match '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
                Write-AtomicJson $ResultPath ([ordered]@{
                    schema_version = 1
                    status = "succeeded"
                    version = $ActiveVersion
                    activation_id = $ActiveActivationId
                    command_id = $ActiveCommandId
                    agent_id = $ConfirmAgentId
                    stable_launcher_retained = $true
                    stable_updater_retained = $true
                })
            }
            $Reported = Get-PlainJsonFile (
                Join-Path $TargetDataRoot ("agent-updates\reported\$ActiveActivationId.json")
            )
            if ($null -ne $Reported -and
                [int]$Reported.schema_version -eq 1 -and
                [string]$Reported.activation_id -eq $ActiveActivationId -and
                [string]$Reported.command_id -eq $ActiveCommandId -and
                [string]$Reported.version -eq $ActiveVersion -and
                [string]$Reported.agent_id -eq [string]$Confirmation.agent_id -and
                $Reported.reported -eq $true) {
                Remove-StagedPayload -JobPath $JobPath
                # The update is fully concluded and reported; every remaining
                # quarantine/incoming leftover is stale by definition.
                Remove-AbandonedUpdateSlots -Root $Root
            }
        }
        return 0
    }

    # Unconfirmed activation: bounded by the pointer's own durable mtime.
    if (-not (Test-RecoveryDeadlineExpired -AnchorUtcFileTime $PointerItem.LastWriteTimeUtc.ToFileTime() `
            -GraceSeconds $RecoveryGraceSeconds)) {
        return 0
    }

    # Deadline expired without confirmation: roll back, exactly like the
    # updater's own synchronous rollback, but driven by the recovery task.
    $JobPath = Join-Path $StagedRoot $ActiveActivationId
    $Previous = Get-PlainJsonFile (Join-Path $JobPath "active-agent.previous.json")
    if ($null -eq $Previous) { return 0 }
    $PreviousVersion = [string]$Previous.version
    if ([int]$Previous.schema_version -ne 1 -or
        $PreviousVersion -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
        [string]$Previous.relative_executable -ne "versions/$PreviousVersion/RigPilotAgent.exe" -or
        [string]$Previous.activation_id -notmatch '^[0-9a-f]{32}$' -or
        [string]$Previous.command_id -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
        return 0
    }
    $PreviousExecutable = Join-Path $Root ("versions\$PreviousVersion\RigPilotAgent.exe")
    if (-not (Test-Path -LiteralPath $PreviousExecutable -PathType Leaf) -or
        ((Get-Item -LiteralPath $PreviousExecutable -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) -ne 0) {
        return 0
    }

    Stop-NewAgentIfOwned -TargetDataRoot $TargetDataRoot `
        -TargetSlotPath (Join-Path $Root ("versions\$ActiveVersion"))

    # The pointer schema carries no agent identity; the readiness marker the
    # updater itself wrote during staging is the durable per-activation
    # source for it.  An unreadable marker still allows the rollback itself
    # (returning the device online outranks the receipt), only the receipt
    # is skipped in that case, exactly like the updater's own best-effort
    # rollback receipt.
    $ReceiptAgentId = ""
    try {
        $Ready = Get-PlainJsonFile (Join-Path $JobPath "updater-ready.json")
        if ($null -ne $Ready -and
            [int]$Ready.schema_version -eq 1 -and
            [string]$Ready.activation_id -eq $ActiveActivationId -and
            [string]$Ready.command_id -eq $ActiveCommandId -and
            [string]$Ready.agent_id -match '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
            $ReceiptAgentId = [string]$Ready.agent_id
        }
    }
    catch {
        $ReceiptAgentId = ""
    }
    $ResultPath = Join-Path $JobPath "activation-result.json"
    if ($ReceiptAgentId -ne "" -and
        -not (Test-Path -LiteralPath $ResultPath -PathType Leaf)) {
        Write-AtomicJson $ResultPath ([ordered]@{
            schema_version = 1
            status = "rolled_back"
            version = $ActiveVersion
            activation_id = $ActiveActivationId
            command_id = $ActiveCommandId
            agent_id = $ReceiptAgentId
            error = "replacement Agent exceeded the bounded activation deadline without confirming two authenticated heartbeats"
        })
    }
    # The durable restart marker precedes the pointer restore: a crash in
    # between leaves the unconfirmed pointer, and the next tick simply
    # repeats these idempotent steps.
    Write-AtomicJson (Join-Path $JobPath "recovery-restart.json") ([ordered]@{
        schema_version = 1
        activation_id = $ActiveActivationId
        version = $ActiveVersion
        restore_version = $PreviousVersion
        restore_relative = [string]$Previous.relative_executable
        attempts = 0
    })
    Restore-FileAtomically (Join-Path $JobPath "active-agent.previous.json") $PointerPath
    $QuarantineRoot = Join-Path $TargetDataRoot "agent-updates\quarantine"
    New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
    $Quarantine = Join-Path $QuarantineRoot ("$ActiveVersion-$ActiveActivationId")
    $RolledBackSlot = Join-Path $Root ("versions\$ActiveVersion")
    if ($ActiveVersion -ne $PreviousVersion -and
        (Test-Path -LiteralPath $RolledBackSlot -PathType Container) -and
        -not (Test-Path -LiteralPath $Quarantine)) {
        [IO.Directory]::Move($RolledBackSlot, $Quarantine)
    }
    try {
        Start-ActiveAgent | Out-Null
    }
    catch {
        # The durable marker drives bounded restart retries on later ticks.
    }
    return 1
}

function Get-PortableUpdaterIdentity {
    # Same folder identity hash as the trusted launcher, so the recovery
    # task name is derived identically in both anchors.
    $NormalizedRoot = $PortableRoot.TrimEnd('\', '/').ToLowerInvariant()
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $Hash = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($NormalizedRoot))
    }
    finally {
        $Hasher.Dispose()
    }
    return -join ($Hash[0..5] | ForEach-Object { $_.ToString("x2") })
}

$RecoveryTaskName = "RigPilotAgentUpdateRecovery-" + (Get-PortableUpdaterIdentity)

function Test-OwnedRecoveryTask($Task) {
    if ($null -eq $Task) {
        return $false
    }
    $TrustedUpdaterPath = Join-Path $PortableRoot "RigPilotAgentUpdater.ps1"
    $ExpectedArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $TrustedUpdaterPath + '" -Action Recover -PortableRoot "' +
        $PortableRoot + '"'
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) {
        return $false
    }
    $TaskAction = $Actions[0]
    # The argument string contains only this installation's own registered
    # content, so an OrdinalIgnoreCase comparison avoids false collisions
    # between the launcher's and the Agent's path spellings of one root.
    if (-not [string]::Equals(
            [string]$TaskAction.Execute,
            $PowerShellPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [string]$TaskAction.WorkingDirectory,
            $PortableRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$TaskAction.Arguments -ne $ExpectedArguments) {
        return $false
    }
    $UserId = [string]$Task.Principal.UserId
    $IsSystem = $false
    if ($UserId -eq "S-1-5-18") {
        $IsSystem = $true
    }
    else {
        try {
            $Sid = (New-Object Security.Principal.NTAccount($UserId)).Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
            $IsSystem = ($Sid -eq "S-1-5-18")
        }
        catch {
            $IsSystem = $false
        }
    }
    return (
        $IsSystem -and
        [string]$Task.Principal.RunLevel -eq "Highest" -and
        ([string]::IsNullOrEmpty([string]$Task.TaskPath) -or
            [string]$Task.TaskPath -eq "\")
    )
}

function Install-PortableRecoveryWatchdog {
    # P3 (user-approved 2026-11-03): the Windows counterpart of the Linux
    # rigpilot-agent-update-recovery timer.  The task runs the TRUSTED root
    # updater (never a staged copy) as SYSTEM every 60s and at boot; the
    # bounded recover logic is a no-op whenever no activation is pending.
    Import-Module ScheduledTasks -ErrorAction Stop
    $ExistingTask = Get-ScheduledTask -TaskName $RecoveryTaskName `
        -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask) {
        if (Test-OwnedRecoveryTask $ExistingTask) { return }
        throw "Scheduled task name collision: $RecoveryTaskName"
    }
    $TrustedUpdaterPath = Join-Path $PortableRoot "RigPilotAgentUpdater.ps1"
    if (-not (Test-Path -LiteralPath $TrustedUpdaterPath -PathType Leaf) -or
        ((Get-Item -LiteralPath $TrustedUpdaterPath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The trusted Agent updater is unavailable."
    }
    $TaskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $TrustedUpdaterPath + '" -Action Recover -PortableRoot "' +
        $PortableRoot + '"'
    $TaskAction = New-ScheduledTaskAction `
        -Execute $PowerShellPath `
        -Argument $TaskArguments `
        -WorkingDirectory $PortableRoot
    $BootTrigger = New-ScheduledTaskTrigger -AtStartup
    $RepeatTrigger = New-ScheduledTaskTrigger -Once `
        -At ([DateTime]::Now.AddSeconds(45)) `
        -RepetitionInterval (New-TimeSpan -Seconds 60)
    # Omit duration for indefinite repetition; MaxValue serializes to a
    # duration that the Windows Task Scheduler rejects at registration.
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -MultipleInstances IgnoreNew
    $Task = New-ScheduledTask `
        -Action $TaskAction `
        -Trigger @($BootTrigger, $RepeatTrigger) `
        -Principal $TaskPrincipal `
        -Settings $TaskSettings `
        -Description "Bound and finish interrupted RigPilot portable Agent updates."
    Register-ScheduledTask -TaskName $RecoveryTaskName -TaskPath "\" `
        -InputObject $Task -Force | Out-Null
    $RegisteredTask = Get-ScheduledTask -TaskName $RecoveryTaskName `
        -TaskPath "\" -ErrorAction Stop
    if (-not (Test-OwnedRecoveryTask $RegisteredTask)) {
        throw "The update recovery task was registered with unexpected arguments."
    }
}

if ($Action -eq "Recover") {
    try {
        if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator
        )) {
            throw "The Agent updater requires an administrator token."
        }
        $ProgramDataRoot = [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::CommonApplicationData
        )
        if ([string]::IsNullOrWhiteSpace($ProgramDataRoot) -or
            -not [IO.Path]::IsPathRooted($ProgramDataRoot)) {
            throw "Windows CommonApplicationData is unavailable."
        }
        $ProgramDataRoot = [IO.Path]::GetFullPath($ProgramDataRoot).TrimEnd('\')
        $ExpectedPortableRoot = [IO.Path]::GetFullPath((Join-Path $ProgramDataRoot "RigPilotAgentPortable")).TrimEnd('\')
        Assert-PlainDirectoryChain $ProgramDataRoot ([IO.Path]::GetPathRoot($ProgramDataRoot)) "ProgramData root"
        if (-not [string]::Equals(
                $PortableRoot, $ExpectedPortableRoot, [StringComparison]::OrdinalIgnoreCase
            )) {
            throw "The Agent updater requires the fixed protected ProgramData root."
        }
        Assert-PlainDirectory $PortableRoot "Portable root"
        Assert-PlainDirectory $DataRoot "Portable data root"
        # A flat first update can restore the root EXE and remove its pointer.
        # Only its verified transaction marker may authorize a restart then.
        exit (Invoke-PortableRecovery -Root $PortableRoot)
    }
    catch {
        Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
        exit 1
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($StagedBundle) -or
        [string]::IsNullOrWhiteSpace($Version) -or
        [string]::IsNullOrWhiteSpace($ActivationId) -or
        [string]::IsNullOrWhiteSpace($AgentId) -or
        [string]::IsNullOrWhiteSpace($CommandId) -or
        [string]::IsNullOrWhiteSpace($ReadyPath) -or
        $AgentPid -le 0 -or
        $LauncherPid -le 0) {
        throw "Activation parameters are incomplete; -Action Recover must not be combined with activate parameters."
    }
    if ($Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
        $ActivationId -notmatch '^[0-9a-f]{32}$' -or
        $AgentId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' -or
        $CommandId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$') {
        throw "Activation metadata is invalid."
    }
    # Initialize activation-only paths after validating that mode's required
    # parameters. Recover never receives StagedBundle; GetFullPath("") throws
    # before its entrypoint could run if performed in the shared prelude.
    $StagedBundle = [IO.Path]::GetFullPath($StagedBundle).TrimEnd('\')
    $ExpectedStagePrefix = [IO.Path]::GetFullPath(
        (Join-Path $DataRoot "agent-updates\staged\$ActivationId")
    ).TrimEnd('\') + '\'
    $StatusPath = Join-Path (Split-Path -Parent $StagedBundle) "activation-result.json"
    $SlotPath = Join-Path $VersionsRoot $Version
    $IncomingSlot = Join-Path $VersionsRoot (".incoming-" + $Version + "-" + $ActivationId)
    $ConfirmationPath = Join-Path $DataRoot ("agent-updates\confirmations\$ActivationId.json")
    $PointerBackup = Join-Path (Split-Path -Parent $StagedBundle) "active-agent.previous.json"
    if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )) {
        throw "The Agent updater requires an administrator token."
    }
    $ProgramDataRoot = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    if ([string]::IsNullOrWhiteSpace($ProgramDataRoot) -or
        -not [IO.Path]::IsPathRooted($ProgramDataRoot)) {
        throw "Windows CommonApplicationData is unavailable."
    }
    $ProgramDataRoot = [IO.Path]::GetFullPath($ProgramDataRoot).TrimEnd('\')
    $ExpectedPortableRoot = [IO.Path]::GetFullPath((Join-Path $ProgramDataRoot "RigPilotAgentPortable")).TrimEnd('\')
    Assert-PlainDirectoryChain $ProgramDataRoot ([IO.Path]::GetPathRoot($ProgramDataRoot)) "ProgramData root"
    if (-not [string]::Equals(
        $PortableRoot, $ExpectedPortableRoot, [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The Agent updater requires the fixed protected ProgramData root."
    }
    Assert-PlainDirectory $PortableRoot "Portable root"
    Assert-PlainDirectory $DataRoot "Portable data root"
    Assert-PlainDirectoryChain $StagedBundle $DataRoot "Staged bundle"
    if (-not $StagedBundle.StartsWith($ExpectedStagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The staged bundle is outside its protected activation directory."
    }
    Assert-StagedManifest
    # Arm the bounded recovery watchdog before this updater takes ownership:
    # if it dies at any later point, the task keeps recovery bounded without
    # requiring any Agent-side capability.  Registration is best-effort here
    # because it must never fail an otherwise healthy activation; the
    # elevated Install-Autostart path enforces it.
    try {
        Install-PortableRecoveryWatchdog
    }
    catch {
        Write-Warning ("The update recovery task could not be registered: " +
            $_.Exception.Message)
    }
    $ExpectedReadyPath = [IO.Path]::GetFullPath(
        (Join-Path (Split-Path -Parent $StagedBundle) "updater-ready.json")
    )
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($ReadyPath),
        $ExpectedReadyPath,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "The updater readiness path is outside this activation job."
    }
    New-BoundRecoveryIntent
    Write-AtomicJson $ExpectedReadyPath ([ordered]@{
        schema_version = 1
        activation_id = $ActivationId
        command_id = $CommandId
        agent_id = $AgentId
        updater_pid = $PID
    })

    # The trusted updater is started before command acknowledgement and waits;
    # this gives the Agent time to report 'staged' to the controller first.
    $HandoffStarted = $true
    Wait-ProcessExit $AgentPid 60
    Wait-ProcessExit $LauncherPid 30

    New-Item -ItemType Directory -Path $VersionsRoot -Force | Out-Null
    Assert-PlainDirectory $VersionsRoot "Versions root"
    # Reclaim staging/quarantine leftovers from earlier concluded updates
    # before the existence checks below: an orphaned .incoming-* slot from a
    # hard-crashed activation would otherwise wedge the same version forever.
    Remove-AbandonedUpdateSlots -Root $PortableRoot
    if (Test-Path -LiteralPath $SlotPath) {
        throw "The target version slot already exists."
    }
    if (Test-Path -LiteralPath $IncomingSlot) {
        throw "The incoming version slot already exists."
    }
    # Native directory moves refuse a destination raced into existence rather
    # than merging into it. Rollback owns only the directory this job moved.
    [IO.Directory]::Move($StagedBundle, $IncomingSlot)
    $OwnedSlotPath = $IncomingSlot
    [IO.Directory]::Move($IncomingSlot, $SlotPath)
    $OwnedSlotPath = $SlotPath

    if (Test-Path -LiteralPath $PointerPath -PathType Leaf) {
        [IO.File]::Copy($PointerPath, $PointerBackup, $true)
    }
    # The root launcher is part of the already-verified update boundary.  Keep
    # it immutable for protocol v1, just like the trusted updater itself.
    # Replacing it from a staging directory changes its Windows owner/ACL and
    # makes the replacement Agent correctly refuse to confirm activation.
    if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf) -or
        ((Get-Item -LiteralPath $LauncherPath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The trusted portable launcher is unavailable."
    }
    $Pointer = [ordered]@{
        schema_version = 1
        version = $Version
        relative_executable = "versions/$Version/RigPilotAgent.exe"
        activation_id = $ActivationId
        command_id = $CommandId
    }
    $IntentMutex = Enter-RecoveryIntentMutex
    try {
        $BeforeActivation = Get-PlainJsonFile (Join-Path (Split-Path -Parent $StagedBundle) "recovery-restart.json")
        if ($null -eq $BeforeActivation -or [string]$BeforeActivation.phase -ne "handoff" -or
            [string]$BeforeActivation.activation_id -cne $ActivationId -or
            -not (Test-SameRecoveryPointer (Get-PlainJsonFile $PointerPath) $BeforeActivation.restore_pointer)) {
            throw "The update handoff was cancelled or superseded."
        }
        Write-AtomicJson $PointerPath $Pointer
        $PointerActivated = $true
    }
    finally { Exit-RecoveryIntentMutex $IntentMutex }

    $NewLauncherProcess = Start-ActiveAgent -RecoveryActivationId $ActivationId
    $Deadline = [DateTime]::UtcNow.AddSeconds(75)
    while ([DateTime]::UtcNow -lt $Deadline) {
        $Confirmation = Test-ConfirmationReceipt -Path $ConfirmationPath `
            -ExpectedVersion $Version -ExpectedActivationId $ActivationId `
            -ExpectedCommandId $CommandId -ExpectedAgentId $AgentId
        if ($null -ne $Confirmation -and $Confirmation -isnot [bool]) {
            # Keep the currently trusted v1 updater and its ACL readiness
            # marker as one immutable pair.  The incoming package must carry
            # an updater for fresh installs, but remote releases never swap
            # executable update logic underneath agent_update.v1.  A future
            # updater protocol must use a new capability/manual bootstrap.
            [void](Complete-ConfirmedActivation)
            exit 0
        }
        if ($null -ne $NewLauncherProcess -and $NewLauncherProcess.HasExited) {
            throw "The replacement Agent launcher exited before confirmation."
        }
        Start-Sleep -Seconds 1
    }
    $Confirmation = Test-ConfirmationReceipt -Path $ConfirmationPath `
        -ExpectedVersion $Version -ExpectedActivationId $ActivationId `
        -ExpectedCommandId $CommandId -ExpectedAgentId $AgentId
    if ($null -ne $Confirmation -and $Confirmation -isnot [bool]) {
        # P2 (user-approved 2026-11-03): a confirmation durably written during
        # the final sleep window was previously never rechecked, so a healthy
        # replacement could be rolled back off-by-one.
        [void](Complete-ConfirmedActivation)
        exit 0
    }
    throw "The replacement Agent did not reconnect before the rollback deadline."
}
catch {
    $Failure = $_.Exception.Message
    if ($ActivationConfirmed) {
        exit 0
    }
    if (-not $HandoffStarted) {
        try {
            if (-not [string]::IsNullOrEmpty($StagedBundle)) {
                $EarlyIntentPath = Join-Path (Split-Path -Parent $StagedBundle) "recovery-restart.json"
                $Early = Get-PlainJsonFile $EarlyIntentPath
                if ($null -ne $Early -and [string]$Early.intent_kind -eq "bound-rollback-restart.v1" -and
                    [string]$Early.activation_id -ceq $ActivationId -and [int]$Early.updater_pid -eq $PID) {
                    Remove-Item -LiteralPath $EarlyIntentPath -Force
                }
            }
        }
        catch { }
        Write-Host "ERROR: $Failure" -ForegroundColor Red
        exit 1
    }
    if ($PointerActivated) {
        Stop-NewAgentIfOwned
    }
    if ($null -ne $NewLauncherProcess -and -not $NewLauncherProcess.HasExited) {
        Stop-Process -Id $NewLauncherProcess.Id -Force -ErrorAction SilentlyContinue
    }
    if ($PointerActivated) {
        if (Test-Path -LiteralPath $PointerBackup -PathType Leaf) {
            Restore-FileAtomically $PointerBackup $PointerPath
        }
        elseif (Test-Path -LiteralPath $PointerPath) {
            Remove-Item -LiteralPath $PointerPath -Force
        }
    }
    if ($null -ne $OwnedSlotPath -and
        (Test-Path -LiteralPath $OwnedSlotPath -PathType Container)) {
        try {
            $QuarantineRoot = Join-Path $DataRoot "agent-updates\quarantine"
            New-Item -ItemType Directory -Path $QuarantineRoot -Force | Out-Null
            $Quarantine = Join-Path $QuarantineRoot ("$Version-$ActivationId")
            if (-not (Test-Path -LiteralPath $Quarantine)) {
                [IO.Directory]::Move($OwnedSlotPath, $Quarantine)
            }
        }
        catch {
            $Failure += "; failed to quarantine rolled-back slot: " + $_.Exception.Message
        }
    }
    try {
        $BoundPath = Join-Path (Split-Path -Parent $StagedBundle) "recovery-restart.json"
        $Bound = Get-PlainJsonFile $BoundPath
        if ($null -ne $Bound -and [string]$Bound.intent_kind -eq "bound-rollback-restart.v1") {
            $IntentMutex = Enter-RecoveryIntentMutex
            try {
                $Bound = Get-PlainJsonFile $BoundPath
                if ($null -ne $Bound -and [string]$Bound.phase -in @("handoff", "rollback")) {
                    $Bound.phase = "rollback"
                    Write-AtomicJson $BoundPath $Bound
                }
            }
            finally { Exit-RecoveryIntentMutex $IntentMutex }
            Invoke-BoundRecoveryRestart -Root $PortableRoot -JobPath (Split-Path -Parent $StagedBundle)
        }
        # Every new handoff has a bound intent. Missing/settled intent is not
        # permission to fall back to an unscoped restart.
    }
    catch {
        $Failure += "; rollback restart failed: " + $_.Exception.Message
    }
    try {
        Write-AtomicJson $StatusPath ([ordered]@{
            schema_version = 1
            status = "rolled_back"
            version = $Version
            activation_id = $ActivationId
            command_id = $CommandId
            agent_id = $AgentId
            error = $Failure
        })
    }
    catch {
        # Preserve the activation failure as the exit status even if local
        # diagnostics cannot be written.
    }
    exit 1
}
