param([Parameter(Mandatory=$true)][string]$Source, [Parameter(Mandatory=$true)][string]$Workspace)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
function Read-Definitions([string]$From, [string[]]$Names) {
    $Tokens = $null
    $Errors = $null
    $Ast = [Management.Automation.Language.Parser]::ParseFile($From, [ref]$Tokens, [ref]$Errors)
    if ($Errors) { throw "Source parse error: $($Errors[0].Message)" }
    $Defs = @($Ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.FunctionDefinitionAst] -and
            (-not $Names -or $Names -contains $_.Name)
    })
    if ($Names -and $Defs.Count -ne $Names.Count) { throw "Required launcher predicates missing." }
    return [ScriptBlock]::Create(($Defs | ForEach-Object { $_.Extent.Text }) -join "`n")
}
# Load definitions only. Do not evaluate product entry statements or invoke Task Scheduler.
. (Read-Definitions (Join-Path $Source 'RigPilotAgentUpdater.ps1') @())
. (Read-Definitions (Join-Path $Source 'portable-agent.ps1') @(
    'Assert-NotReparsePoint', 'Resolve-PortableAgentExe', 'Cancel-PortableUpdateRecoveryIntent', 'Test-PortableRecoveryLaunchAllowed'
))

# All process boundaries are local fakes; only the product's marker/receipt/
# identity and trusted-path code touches the per-case temporary installation.
function Get-Process {
    [CmdletBinding()] param([int]$Id)
    if ($Id -eq $PID) {
        if ($script:UpdaterAlive) {
            return [pscustomobject]@{Id=$PID;Path=$script:PowerShellPath;StartTime=$script:UpdaterStart}
        }
        return
    }
    if ($Id -ne $script:AgentPid) { throw "Unexpected PID lookup $Id" }
    if ($script:OriginalAlive) {
        return [pscustomobject]@{Id=$Id; Path=$script:OldExecutable; StartTime=$script:OriginalStart}
    }
}
function Test-PortableAgentRunning([string]$TargetDataRoot, [string]$ExpectedExecutable) {
    return ($script:OtherOldRunning -and $ExpectedExecutable -eq $script:OldExecutable)
}
function Start-ActiveAgent([string]$RecoveryActivationId='') {
    if ($RecoveryActivationId -cne $script:ActivationId) { throw 'Launch missing exact transaction' }
    $script:LaunchCount++
}
function Stop-NewAgentIfOwned([string]$TargetDataRoot, [string]$TargetSlotPath) {
    if ($TargetSlotPath -ne (Join-Path $script:PortableRoot "versions\$script:Version")) { throw 'Unexpected stop target' }
    $script:StopCount++
}
function Check([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Make-Pointer([string]$V, [string]$Act, [string]$Cmd) {
    return [pscustomobject]@{schema_version=1;version=$V;relative_executable="versions/$V/RigPilotAgent.exe";activation_id=$Act;command_id=$Cmd}
}
function Begin-Case([string]$Case) {
    $script:PortableRoot = Join-Path $Workspace $Case
    $script:DataRoot = Join-Path $script:PortableRoot 'data'
    $script:DataDir = $script:DataRoot
    $script:StatePath = Join-Path $script:DataRoot 'state.json'
    $script:PointerPath = Join-Path $script:DataRoot 'active-agent.json'
    $script:OldVersion = '0.8.61'
    $script:Version = '0.8.62'
    $script:AgentId = [guid]::NewGuid().ToString('D')
    $script:ActivationId = [guid]::NewGuid().ToString('N')
    $script:CommandId = [guid]::NewGuid().ToString('D')
    $script:AgentPid = 14135
    $script:OriginalStart = [datetime]::UtcNow.AddMinutes(-10)
    $script:UpdaterStart = [datetime]::UtcNow.AddMinutes(-2)
    $script:UpdaterAlive = $true
    $script:PowerShellPath = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $script:OriginalAlive = $true
    $script:OtherOldRunning = $false
    $script:LaunchCount = 0
    $script:StopCount = 0
    $script:RecoveryGraceSeconds = 195
    $script:RecoveryRestartAttemptLimit = 5
    $script:RecoveryClockResetToleranceTicks = 50000000L
    $script:Job = Join-Path $script:DataRoot "agent-updates\staged\$script:ActivationId"
    $script:StagedBundle = Join-Path $script:Job 'bundle'
    $script:Marker = Join-Path $script:Job 'recovery-restart.json'
    $script:StatusPath = Join-Path $script:Job 'activation-result.json'
    [void][IO.Directory]::CreateDirectory($script:StagedBundle)
    $script:SlotPath = Join-Path $script:PortableRoot "versions\$script:Version"
    [void][IO.Directory]::CreateDirectory($script:SlotPath)
    foreach ($destination in @($script:SlotPath, $script:StagedBundle)) {
        $file = Join-Path $destination 'RigPilotAgent.exe'
        [IO.File]::WriteAllText($file, 'inert target executable fixture')
        $fileHash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-AtomicJson (Join-Path $destination 'agent-release.json') ([ordered]@{
            schema_version=1;version=$script:Version;platform='windows';architecture='x86_64';files=@{'RigPilotAgent.exe'=$fileHash}
        })
    }
    $script:Prior = $null
    $dir = $script:PortableRoot
    if ($Case -in @('slot-from-candidate','superseded-pointer')) {
        $script:Prior = Make-Pointer $script:OldVersion ([guid]::NewGuid().ToString('N')) ([guid]::NewGuid().ToString('D'))
        $dir = Join-Path $script:PortableRoot "versions\$script:OldVersion"
        [void][IO.Directory]::CreateDirectory($dir)
        Write-AtomicJson $script:PointerPath $script:Prior
    }
    $script:OldExecutable = Join-Path $dir 'RigPilotAgent.exe'
    $script:AgentExe = $script:OldExecutable
    [IO.File]::WriteAllText($script:OldExecutable, 'inert previous executable fixture')
    $digest = (Get-FileHash -LiteralPath $script:OldExecutable -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-AtomicJson (Join-Path $dir 'agent-release.json') ([ordered]@{
        schema_version=1;platform='windows';architecture='x86_64';version=$script:OldVersion;files=@{'RigPilotAgent.exe'=$digest}
    })
    $script:Saved = [ordered]@{agent_id=$script:AgentId;agent_secret=('rpa_'+('a'*48))}
    Write-AtomicJson $script:StatePath $script:Saved
    if ($Case -ne 'absent') {
        New-BoundRecoveryIntent
        $created = Get-PlainJsonFile $script:Marker
        Check ($created.intent_kind -ceq 'bound-rollback-restart.v1' -and $created.restore_sha256 -ceq $digest) 'Missing or unbound intent'
        Check (-not ([IO.File]::ReadAllText($script:Marker)).Contains($script:Saved.agent_secret)) 'Marker contains reusable credential'
        if ($Case -ne 'recent-handoff') {
            $created.created_ticks = [datetime]::UtcNow.AddMinutes(-10).ToFileTime()
            Write-AtomicJson $script:Marker $created
        }
    }
    $script:OriginalAlive = ($Case -in @('original-running','expired-handoff-old-survived'))
    # For all genuine expired-handoff cases the original updater has exited;
    # a recycled/unrelated updater PID must not count as the owner.
    $script:UpdaterAlive = ($Case -in @('recent-handoff','original-running','candidate-launch','candidate-launch-after-stop'))
}

function Exercise([string]$Case) {
    Begin-Case $Case
    $pending = Make-Pointer $Version $ActivationId $CommandId
    $unrelated = Make-Pointer '0.8.63' ([guid]::NewGuid().ToString('N')) ([guid]::NewGuid().ToString('D'))
    if ($Case -in @('candidate-launch','candidate-launch-after-stop')) {
        Write-AtomicJson $PointerPath $pending
        $script:AgentExe = Join-Path $SlotPath 'RigPilotAgent.exe'
        Check (Test-PortableRecoveryLaunchAllowed $ActivationId) 'Verified candidate could not start'
        if ($Case -eq 'candidate-launch-after-stop') {
            Cancel-PortableUpdateRecoveryIntent
            Check (-not (Test-PortableRecoveryLaunchAllowed $ActivationId)) 'Cancelled candidate authorization survived'
        }
        return
    }
    if ($Case -eq 'superseded-before-delayed-launch') {
        Invoke-BoundRecoveryRestart -Root $PortableRoot -JobPath $Job
        Check ($script:LaunchCount -eq 1) 'Missing initial rollback launch'
        # Delayed launcher cached $AgentExe before waiting for the common lock.
        $later = Join-Path $PortableRoot 'versions\0.8.63'
        [void][IO.Directory]::CreateDirectory($later)
        [IO.File]::WriteAllText((Join-Path $later 'RigPilotAgent.exe'), 'valid later slot fixture')
        Write-AtomicJson $PointerPath $unrelated
        Check (-not (Test-PortableRecoveryLaunchAllowed $ActivationId)) 'Superseded pointer authorized delayed old launcher'
        return
    }
    if ($Case -eq 'large-settled-history') {
        $staged = Split-Path -Parent $Job
        for ($idx = 1; $idx -le 535; $idx++) {
            $jobName = '{0:x32}' -f $idx
            $older = Join-Path $staged $jobName
            [void][IO.Directory]::CreateDirectory($older)
            if ($idx -le 20) {
                Write-AtomicJson (Join-Path $older 'recovery-restart.json') ([ordered]@{
                    schema_version=1; intent_kind='bound-rollback-restart.v1'; activation_id=$jobName; phase='cancelled'
                })
            }
        }
        [IO.Directory]::SetLastWriteTimeUtc($Job, [DateTime]::UtcNow.AddHours(-3))
        [void](Invoke-PortableRecovery -Root $PortableRoot)
        Check ($script:LaunchCount -eq 1) 'Completed/unmarked history hid the only live transaction'
        Check ((Get-PlainJsonFile $StatusPath).command_id -ceq $CommandId) 'Recovery chose an unrelated job'
        return
    }
    switch ($Case) {
        'changed-agent' {$Saved.agent_id = [guid]::NewGuid().ToString('D'); Write-AtomicJson $StatePath $Saved}
        'rotated-secret' {$Saved.agent_secret = 'rpa_'+('b'*48); Write-AtomicJson $StatePath $Saved}
        'changed-executable' {[IO.File]::AppendAllText($OldExecutable,' modified')}
        'superseded-pointer' {Write-AtomicJson $PointerPath $unrelated}
        'flat-from-candidate' {Write-AtomicJson $PointerPath $pending}
        'slot-from-candidate' {Write-AtomicJson $PointerPath $pending}
        'verified-old-running' {
            $script:OtherOldRunning = $true
            $v = Get-PlainJsonFile $Marker
            $v.phase = 'rollback'; Write-AtomicJson $Marker $v
        }
        'confirmed-candidate' {
            Write-AtomicJson $PointerPath $pending
            Write-AtomicJson (Join-Path $DataRoot "agent-updates\confirmations\$ActivationId.json") ([ordered]@{
                schema_version=1;activation_id=$ActivationId;command_id=$CommandId;agent_id=$AgentId;version=$Version;health_heartbeats=2
            })
        }
        'operator-cancel' {Cancel-PortableUpdateRecoveryIntent}
    }
    $ticks = if ($Case -eq 'attempt-bound') { $RecoveryRestartAttemptLimit + 2 } else { 1 }
    for ($i = 0; $i -lt $ticks; $i++) { Invoke-BoundRecoveryRestart -Root $PortableRoot -JobPath $Job }
    if ($Case -eq 'expired-handoff-old-survived') {
        Check ($script:LaunchCount -eq 0) 'Running original was relaunched'
        $script:OriginalAlive = $false
        Invoke-BoundRecoveryRestart -Root $PortableRoot -JobPath $Job
        Check ($script:LaunchCount -eq 0) 'Expired handoff revived a later independent stop'
        return
    }
    $starts = if ($Case -eq 'attempt-bound') { $RecoveryRestartAttemptLimit }
              elseif ($Case -in @('flat-after-handoff','flat-from-candidate','slot-from-candidate','delayed-launch-after-cancel')) { 1 }
              else { 0 }
    Check ($script:LaunchCount -eq $starts) "Expected $starts launches, saw $script:LaunchCount"
    $expectedStops = if ($Case -in @('flat-from-candidate','slot-from-candidate')) { 1 } else { 0 }
    Check ($script:StopCount -eq $expectedStops) "Expected $expectedStops candidate stops, saw $script:StopCount"
    $now = Get-PlainJsonFile $PointerPath
    $receipt = Get-PlainJsonFile $StatusPath
    if ($starts) {
        $v = Get-PlainJsonFile $Marker
        Check ($v.attempts -eq $starts -and $v.phase -ceq 'rollback') 'Invalid bounded retry counter'
        Check ($receipt.status -ceq 'rolled_back' -and $receipt.activation_id -ceq $ActivationId -and $receipt.command_id -ceq $CommandId) 'Wrong transaction receipt'
        Check (Test-SameRecoveryPointer $now $Prior) 'Previous pointer not restored'
        Check (Test-PortableRecoveryLaunchAllowed $ActivationId) 'Original scoped launch was incorrectly refused'
        if ($Case -eq 'delayed-launch-after-cancel') {
            Cancel-PortableUpdateRecoveryIntent
            Check (-not (Test-PortableRecoveryLaunchAllowed $ActivationId)) 'Queued launch survived explicit stop'
        }
    } else {
        Check ($null -eq $receipt) 'Rejected/no-op transaction wrote a rollback result'
        if ($Case -eq 'superseded-pointer') { Check (Test-SameRecoveryPointer $now $unrelated) 'Later pointer overwritten' }
        if ($Case -eq 'confirmed-candidate') { Check (Test-SameRecoveryPointer $now $pending) 'Confirmed pointer lost' }
        if ($Case -in @('confirmed-candidate','verified-old-running','absent')) { Check (-not (Test-Path -LiteralPath $Marker)) 'Obsolete intent retained' }
        if ($Case -eq 'operator-cancel') { Check (-not (Test-PortableRecoveryLaunchAllowed $ActivationId)) 'Cancelled launch authorized' }
    }
}

$outcomes = [ordered]@{}
foreach ($s in @('absent','recent-handoff','flat-after-handoff','flat-from-candidate','slot-from-candidate',
  'original-running','verified-old-running','attempt-bound','changed-agent','rotated-secret','changed-executable',
  'superseded-pointer','confirmed-candidate','operator-cancel','delayed-launch-after-cancel','candidate-launch',
  'candidate-launch-after-stop','superseded-before-delayed-launch','expired-handoff-old-survived','large-settled-history')) {
    try { Exercise $s; $outcomes[$s] = 'passed' }
    catch { $outcomes[$s] = ($_.Exception.Message + ' @ '+ $_.InvocationInfo.ScriptLineNumber) }
}
$outcomes | ConvertTo-Json -Compress
