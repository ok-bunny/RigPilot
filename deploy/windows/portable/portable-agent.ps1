[CmdletBinding()]
param(
    [ValidateSet("Start", "Stop", "InstallAutostart", "RemoveAutostart", "CheckConnection")]
    [string]$Action = "Start",

    [switch]$Scheduled,

    # Used only by Install-Autostart.bat through Start-RigPilotAgent.bat.  It
    # completes the same interactive config/enrollment preparation as Start,
    # but does not leave a foreground Agent running before migration.
    [switch]$BootstrapEnrollment,

    # Internal coordinator call: failures must return to the migration
    # transaction instead of entering a foreground run that never returns.
    [switch]$MigrationInstall
)

$ErrorActionPreference = "Stop"
$PortableRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$DataDir = Join-Path $PortableRoot "data"
$ConfigPath = Join-Path $DataDir "config.json"
$StatePath = Join-Path $DataDir "state.json"
$PidPath = Join-Path $DataDir "agent.pid.json"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Resolve-PortableAgentExe {
    $Fallback = Join-Path $PortableRoot "RigPilotAgent.exe"
    $PointerPath = Join-Path $DataDir "active-agent.json"
    if (-not (Test-Path -LiteralPath $PointerPath -PathType Leaf)) {
        return $Fallback
    }
    $PointerItem = Get-Item -LiteralPath $PointerPath -Force
    if (($PointerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $PointerItem.Length -gt 32768) {
        throw "The active Agent pointer is unsafe."
    }
    $Pointer = Get-Content -LiteralPath $PointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Names = @($Pointer.PSObject.Properties.Name | Sort-Object)
    $ExpectedNames = @("activation_id", "command_id", "relative_executable", "schema_version", "version")
    $Version = [string]$Pointer.version
    $ExpectedRelative = "versions/$Version/RigPilotAgent.exe"
    if ((Compare-Object $Names $ExpectedNames).Count -ne 0 -or
        [int]$Pointer.schema_version -ne 1 -or
        $Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
        [string]$Pointer.activation_id -notmatch '^[0-9a-f]{32}$' -or
        [string]$Pointer.command_id -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' -or
        [string]$Pointer.relative_executable -ne $ExpectedRelative) {
        throw "The active Agent pointer is invalid."
    }
    $Candidate = [IO.Path]::GetFullPath(
        (Join-Path $PortableRoot ($ExpectedRelative.Replace('/', '\')))
    )
    $VersionsPrefix = [IO.Path]::GetFullPath((Join-Path $PortableRoot "versions")).TrimEnd('\') + '\'
    if (-not $Candidate.StartsWith($VersionsPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw "The active Agent version slot is unavailable."
    }
    $CandidateItem = Get-Item -LiteralPath $Candidate -Force
    if (($CandidateItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "The active Agent executable is unsafe."
    }
    return $Candidate
}

$AgentExe = $null
$ActiveAgentPointerRepairNeeded = $false
$ActiveAgentPointerFailure = $null
try {
    $AgentExe = Resolve-PortableAgentExe
}
catch {
    # A failed activation may leave a plain pointer whose version slot was
    # never committed.  Start remains fail-closed, but a deliberate elevated
    # Install-Autostart must be able to repair the protected installation from
    # a complete overlaid package instead of being permanently locked out.
    $Fallback = Join-Path $PortableRoot "RigPilotAgent.exe"
    $PointerPath = Join-Path $DataDir "active-agent.json"
    $CanRepair = $Action -eq "InstallAutostart" -and
        (Test-Path -LiteralPath $Fallback -PathType Leaf) -and
        (Test-Path -LiteralPath $PointerPath -PathType Leaf)
    if ($CanRepair) {
        $FallbackItem = Get-Item -LiteralPath $Fallback -Force
        $PointerItem = Get-Item -LiteralPath $PointerPath -Force
        $CanRepair = (($FallbackItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) -and
            (($PointerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0) -and
            $PointerItem.Length -le 32768
    }
    if (-not $CanRepair) {
        throw
    }
    $AgentExe = $Fallback
    $ActiveAgentPointerRepairNeeded = $true
    $ActiveAgentPointerFailure = $_.Exception.Message
}

function Test-IsAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Restart-Elevated(
    [string]$RequestedAction,
    [bool]$RequestedScheduled,
    [bool]$RequestedBootstrapEnrollment
) {
    $ScriptPath = [IO.Path]::GetFullPath($script:PSCommandPath)
    $ScriptItem = Get-Item -LiteralPath $ScriptPath -Force
    $ScriptRoot = Get-Item -LiteralPath (Split-Path -Parent $ScriptPath) -Force
    if (($ScriptItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($ScriptRoot.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Portable launcher must not be a symbolic link or junction."
    }
    # Windows PowerShell 5.1 joins a Start-Process string[] itself and can
    # discard quoting around -File paths containing spaces.  Pass one fully
    # formed command line instead so the elevated process receives exactly
    # the same action and script path on Windows 10 and Windows 11.
    $ElevationArguments = '-NoProfile -ExecutionPolicy Bypass -File "' +
        $ScriptPath + '" -Action ' + $RequestedAction
    if ($RequestedScheduled) {
        $ElevationArguments += " -Scheduled"
    }
    if ($RequestedBootstrapEnrollment) {
        $ElevationArguments += " -BootstrapEnrollment"
    }
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $Elevated = Start-Process `
        -FilePath $PowerShellPath `
        -ArgumentList $ElevationArguments `
        -WorkingDirectory $PortableRoot `
        -Verb RunAs `
        -PassThru
    if ($null -eq $Elevated) {
        throw "Administrator approval was not granted."
    }
    # Wait for every action, including Start.  A successful Start naturally
    # waits while the Agent is running; an enrollment/startup failure is then
    # returned to the original BAT instead of being lost when the UAC child
    # window closes.
    $Elevated.WaitForExit()
    $Elevated.Refresh()
    return [int]$Elevated.ExitCode
}

function Assert-NotReparsePoint([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a symbolic link or junction: $Path"
    }
}

function Get-TrustedProgramDataRoot {
    $KnownFolder = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::CommonApplicationData
    )
    if ([string]::IsNullOrWhiteSpace($KnownFolder) -or
        -not [IO.Path]::IsPathRooted($KnownFolder)) {
        throw "Windows CommonApplicationData is unavailable."
    }
    $Root = [IO.Path]::GetFullPath($KnownFolder).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Windows CommonApplicationData is unavailable."
    }
    $Current = Get-Item -LiteralPath $Root -Force
    while ($null -ne $Current) {
        if (($Current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Windows CommonApplicationData contains a link or junction."
        }
        $Parent = [IO.Directory]::GetParent($Current.FullName)
        if ($null -eq $Parent) { break }
        $Current = Get-Item -LiteralPath $Parent.FullName -Force
    }
    return $Root
}
function Install-TemporaryFile([string]$TemporaryPath, [string]$DestinationPath) {
    # Move-Item -Force cannot reliably replace an existing file on older
    # Windows 10 / Windows PowerShell 5.1 combinations.  File.Replace is the
    # native same-volume atomic replacement operation and preserves the old
    # config when replacement fails.
    if (Test-Path -LiteralPath $DestinationPath -PathType Leaf) {
        $BackupPath = Join-Path `
            (Split-Path -Parent $DestinationPath) `
            (".config.backup." + [Guid]::NewGuid().ToString("N") + ".tmp")
        try {
            # .NET Framework (used by Windows PowerShell 5.1) rejects a null
            # backup path even though newer runtimes accept one.
            [IO.File]::Replace($TemporaryPath, $DestinationPath, $BackupPath, $true)
        }
        catch {
            if (-not (Test-Path -LiteralPath $DestinationPath -PathType Leaf) -and
                (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
                [IO.File]::Move($BackupPath, $DestinationPath)
            }
            throw
        }
        finally {
            if (Test-Path -LiteralPath $BackupPath -PathType Leaf) {
                Remove-Item -LiteralPath $BackupPath -Force
            }
        }
        return
    }
    [IO.File]::Move($TemporaryPath, $DestinationPath)
}

function Get-WindowsProcessInfo(
    [string]$Filter,
    [string]$FallbackName,
    [Nullable[int]]$FallbackId = $null
) {
    # Get-CimInstance is normally present on Windows 10, but it can be
    # unavailable or broken when WMI/CIM components have been trimmed or are
    # still starting during boot.  Windows PowerShell 5.1 still provides the
    # WMI cmdlet, and Get-Process is a final local fallback.
    if ($null -ne (Get-Command -Name Get-CimInstance -ErrorAction SilentlyContinue)) {
        try {
            return @(Get-CimInstance -ClassName Win32_Process -Filter $Filter -ErrorAction Stop)
        }
        catch {
            Write-Verbose "Get-CimInstance failed: $($_.Exception.Message)"
        }
    }
    if ($null -ne (Get-Command -Name Get-WmiObject -ErrorAction SilentlyContinue)) {
        try {
            return @(Get-WmiObject -Class Win32_Process -Filter $Filter -ErrorAction Stop)
        }
        catch {
            Write-Verbose "Get-WmiObject failed: $($_.Exception.Message)"
        }
    }

    if ($null -ne $FallbackId) {
        $Processes = @(Get-Process -Id ([int]$FallbackId) -ErrorAction SilentlyContinue)
    }
    else {
        $Processes = @(Get-Process -Name $FallbackName -ErrorAction SilentlyContinue)
    }
    $Result = @()
    foreach ($Process in $Processes) {
        $ExecutablePath = $null
        try {
            $ExecutablePath = [string]$Process.Path
        }
        catch {
            try {
                $ExecutablePath = [string]$Process.MainModule.FileName
            }
            catch {
                continue
            }
        }
        $Result += [PSCustomObject]@{
            ProcessId = $Process.Id
            ExecutablePath = $ExecutablePath
        }
    }
    return $Result
}

function Get-PortableIdentity {
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

$PortableIdentity = Get-PortableIdentity
$AutostartTaskName = "RigPilotAgentPortable-$PortableIdentity"
$RecoveryTaskName = "RigPilotAgentUpdateRecovery-$PortableIdentity"
$LaunchMutexName = "Global\RigPilotAgentPortableLaunch-$PortableIdentity"

function Enter-PortableLaunchMutex {
    $CreatedNew = $false
    $Mutex = New-Object System.Threading.Mutex($false, $LaunchMutexName, [ref]$CreatedNew)
    try {
        try {
            $Acquired = $Mutex.WaitOne([TimeSpan]::FromSeconds(15))
        }
        catch [System.Threading.AbandonedMutexException] {
            $Acquired = $true
        }
        if (-not $Acquired) {
            throw "Another RigPilot portable launcher is still starting. Try again shortly."
        }
        return $Mutex
    }
    catch {
        $Mutex.Dispose()
        throw
    }
}

function Exit-PortableLaunchMutex([Threading.Mutex]$Mutex) {
    if ($null -eq $Mutex) {
        return
    }
    try {
        $Mutex.ReleaseMutex()
    }
    finally {
        $Mutex.Dispose()
    }
}

function Test-SystemTaskPrincipal($Principal) {
    if ($null -eq $Principal) {
        return $false
    }
    $UserId = [string]$Principal.UserId
    if ($UserId -eq "S-1-5-18") {
        return $true
    }
    try {
        $Sid = (New-Object Security.Principal.NTAccount($UserId)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
        return $Sid -eq "S-1-5-18"
    }
    catch {
        return $false
    }
}

function Test-OwnedAutostartTask($Task) {
    if ($null -eq $Task) {
        return $false
    }
    $ScriptPath = [IO.Path]::GetFullPath($script:PSCommandPath)
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $ExpectedArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $ScriptPath + '" -Action Start -Scheduled'
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) {
        return $false
    }
    $TaskAction = $Actions[0]
    return (
        [string]::Equals(
            [string]$TaskAction.Execute,
            $PowerShellPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]::Equals(
            [string]$TaskAction.WorkingDirectory,
            $PortableRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -and
        [string]$TaskAction.Arguments -ceq $ExpectedArguments -and
        (Test-SystemTaskPrincipal $Task.Principal) -and
        [string]$Task.Principal.RunLevel -eq "Highest" -and
        ([string]::IsNullOrEmpty([string]$Task.TaskPath) -or
            [string]$Task.TaskPath -eq "\")
    )
}

function Test-OwnedRecoveryTask($Task) {
    # P3 (user-approved 2026-11-03): the Windows counterpart of the Linux
    # rigpilot-agent-update-recovery timer.  The task must run the TRUSTED
    # root updater (never a staged copy) as SYSTEM in bounded recover mode.
    if ($null -eq $Task) {
        return $false
    }
    $UpdaterPath = [IO.Path]::GetFullPath(
        (Join-Path $PortableRoot "RigPilotAgentUpdater.ps1")
    )
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $ExpectedArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $UpdaterPath + '" -Action Recover -PortableRoot "' +
        $PortableRoot + '"'
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) {
        return $false
    }
    $TaskAction = $Actions[0]
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
    # Idempotent registration of the bounded update-recovery task.  The task
    # itself performs no work unless an activation pointer is pending, so a
    # missing update never touches the running Agent.  The arguments must
    # stay byte-compatible with the trusted updater's own ownership check.
    Import-Module ScheduledTasks -ErrorAction Stop
    $ExistingTask = Get-ScheduledTask -TaskName $RecoveryTaskName `
        -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask) {
        if (Test-OwnedRecoveryTask $ExistingTask) { return }
        throw "Scheduled task name collision: $RecoveryTaskName"
    }
    $UpdaterPath = [IO.Path]::GetFullPath(
        (Join-Path $PortableRoot "RigPilotAgentUpdater.ps1")
    )
    if (-not (Test-Path -LiteralPath $UpdaterPath -PathType Leaf) -or
        ((Get-Item -LiteralPath $UpdaterPath -Force).Attributes -band
            [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "A required portable update component is missing."
    }
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $TaskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $UpdaterPath + '" -Action Recover -PortableRoot "' +
        $PortableRoot + '"'
    $TaskAction = New-ScheduledTaskAction `
        -Execute $PowerShellPath `
        -Argument $TaskArguments `
        -WorkingDirectory $PortableRoot
    $BootTrigger = New-ScheduledTaskTrigger -AtStartup
    $RepeatTrigger = New-ScheduledTaskTrigger -Once `
        -At ([DateTime]::Now.AddSeconds(45)) `
        -RepetitionInterval (New-TimeSpan -Seconds 60)
    # An omitted duration means indefinite repetition. TimeSpan.MaxValue is
    # serialized by the cmdlet as P99999999DT23H59M59S, rejected by Windows.
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

function New-PortableBoundaryAcl([bool]$AllowUsersRead) {
    $SystemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $AdministratorsSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
    $UsersSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-32-545")
    $Inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $Propagation = [Security.AccessControl.PropagationFlags]::None
    $Allow = [Security.AccessControl.AccessControlType]::Allow
    $Acl = [Security.AccessControl.DirectorySecurity]::new()
    $Acl.SetAccessRuleProtection($true, $false)
    $Acl.SetOwner($AdministratorsSid)
    foreach ($Sid in @($SystemSid, $AdministratorsSid)) {
        $Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $Sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $Inheritance,
            $Propagation,
            $Allow
        ))
    }
    if ($AllowUsersRead) {
        $Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $UsersSid,
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            $Inheritance,
            $Propagation,
            $Allow
        ))
    }
    return $Acl
}

function Assert-PortableProtectedAcl([string]$Path, [bool]$RequireProtected = $true) {
    $Acl = Get-Acl -LiteralPath $Path
    try {
        $OwnerSid = (New-Object Security.Principal.NTAccount($Acl.Owner)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        $OwnerSid = [string]$Acl.Owner
    }
    if ($OwnerSid -ne "S-1-5-32-544" -or
        ($RequireProtected -and -not $Acl.AreAccessRulesProtected)) {
        throw "Portable update path is not protected by the local Administrators group: $Path"
    }
    $TrustedWriteSids = @("S-1-5-18", "S-1-5-32-544")
    $TrustedFull = @{
        "S-1-5-18" = $false
        "S-1-5-32-544" = $false
    }
    # Atomic write/delete/ACL rights only.  Composite Modify/FullControl also
    # contain read bits and would incorrectly reject the intended Users RX ACE.
    $DangerousRights = [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    $FullControl = [Security.AccessControl.FileSystemRights]::FullControl
    foreach ($Rule in @($Acl.Access)) {
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        try {
            $Sid = $Rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            throw "Portable update ACL contains an unreadable principal: $Path"
        }
        if ($Sid -in $TrustedWriteSids -and
            (($Rule.FileSystemRights -band $FullControl) -eq $FullControl)) {
            $TrustedFull[$Sid] = $true
        }
        elseif (($Rule.FileSystemRights -band $DangerousRights) -ne 0) {
            throw "Portable update path remains writable by an untrusted principal: $Path"
        }
    }
    if (-not $TrustedFull["S-1-5-18"] -or -not $TrustedFull["S-1-5-32-544"]) {
        throw "Portable update path is missing trusted full-control entries: $Path"
    }
}

function Repair-PortableAclChildren([string]$ParentPath, [string]$IcaclsPath) {
    foreach ($ChildPath in @([IO.Directory]::EnumerateFileSystemEntries($ParentPath))) {
        $Attributes = [IO.File]::GetAttributes($ChildPath)
        if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Portable update tree must not contain links or junctions: $ChildPath"
        }
        foreach ($Arguments in @(
            @($ChildPath, "/setowner", "*S-1-5-32-544", "/C", "/Q"),
            @($ChildPath, "/inheritance:e", "/C", "/Q"),
            @($ChildPath, "/reset", "/C", "/Q")
        )) {
            & $IcaclsPath @Arguments | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Could not repair a portable child ACL (icacls exit $LASTEXITCODE)."
            }
        }
        if (($Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
            Repair-PortableAclChildren $ChildPath $IcaclsPath
        }
    }
}

function Get-PortablePlainPaths([string]$Root, [string]$Label) {
    Assert-NotReparsePoint $Root "$Label directory"
    $Prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $Stack = New-Object System.Collections.Stack
    $Stack.Push($Root)
    $Paths = New-Object System.Collections.Generic.List[string]
    $Paths.Add($Root)
    while ($Stack.Count -gt 0) {
        $Directory = [string]$Stack.Pop()
        foreach ($Entry in @([IO.Directory]::EnumerateFileSystemEntries($Directory))) {
            if ($Paths.Count -ge 200000) {
                throw "$Label tree contains too many entries."
            }
            $Full = [IO.Path]::GetFullPath($Entry)
            if (-not $Full.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "$Label path escapes its directory."
            }
            $Attributes = [IO.File]::GetAttributes($Entry)
            if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a link or junction: $Entry"
            }
            $Paths.Add($Full)
            if (($Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                $Stack.Push($Full)
            }
        }
    }
    return $Paths.ToArray()
}

function Assert-PortablePrivateAcl([string]$Path, [bool]$Boundary) {
    $Acl = Get-Acl -LiteralPath $Path
    try {
        $OwnerSid = (New-Object Security.Principal.NTAccount($Acl.Owner)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        $OwnerSid = [string]$Acl.Owner
    }
    if ($OwnerSid -ne "S-1-5-32-544" -or
        ($Boundary -and -not $Acl.AreAccessRulesProtected)) {
        throw "Portable first-contact data ACL owner or inheritance is unsafe: $Path"
    }
    $TrustedFull = @{
        "S-1-5-18" = $false
        "S-1-5-32-544" = $false
    }
    $FullControl = [Security.AccessControl.FileSystemRights]::FullControl
    foreach ($Rule in @($Acl.Access)) {
        if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        try {
            $Sid = $Rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            throw "Portable first-contact data ACL contains an unreadable principal: $Path"
        }
        if (-not $TrustedFull.ContainsKey($Sid)) {
            throw "Portable first-contact data remains readable by an untrusted principal: $Path"
        }
        if (($Rule.FileSystemRights -band $FullControl) -eq $FullControl) {
            $TrustedFull[$Sid] = $true
        }
    }
    if (-not $TrustedFull["S-1-5-18"] -or -not $TrustedFull["S-1-5-32-544"]) {
        throw "Portable first-contact data is missing trusted full-control entries: $Path"
    }
}

function Assert-PortableSourceParentChain {
    $CurrentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $TrustedSids = @("S-1-5-18", "S-1-5-32-544", $CurrentUserSid)
    # The NTFS volume root can be owned by Windows Modules Installer. Trust
    # this exact service SID only on the protected volume root, never on a
    # source parent or an arbitrary service-owned directory.
    $TrustedInstallerSid = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    $VolumeRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($PortableRoot))
    $Current = [IO.Directory]::GetParent($PortableRoot)
    $ImmediateParent = $true
    while ($null -ne $Current) {
        $Path = $Current.FullName
        $Attributes = [IO.File]::GetAttributes($Path)
        if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Portable first-contact source parent contains a link or junction."
        }
        $Acl = Get-Acl -LiteralPath $Path
        try {
            $OwnerSid = (New-Object Security.Principal.NTAccount($Acl.Owner)).Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            $OwnerSid = [string]$Acl.Owner
        }
        $ProtectedInstallerVolumeRoot = (
            $OwnerSid -ceq $TrustedInstallerSid -and
            $Acl.AreAccessRulesProtected -and
            [string]::Equals(
                $Path.TrimEnd('\'),
                $VolumeRoot.TrimEnd('\'),
                [StringComparison]::OrdinalIgnoreCase
            )
        )
        if ($OwnerSid -notin $TrustedSids -and -not $ProtectedInstallerVolumeRoot) {
            throw "Portable first-contact source parent has an untrusted owner. Extract into an administrator-owned private local NTFS directory. Parent: $Path"
        }
        $ReplaceRights = [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
            [Security.AccessControl.FileSystemRights]::Delete -bor
            [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
            [Security.AccessControl.FileSystemRights]::TakeOwnership
        $CreateRights = [Security.AccessControl.FileSystemRights]::WriteData -bor
            [Security.AccessControl.FileSystemRights]::AppendData -bor
            [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
            [Security.AccessControl.FileSystemRights]::WriteAttributes
        $DangerousRights = if ($ImmediateParent) { $ReplaceRights -bor $CreateRights } else { $ReplaceRights }
        foreach ($Rule in @($Acl.Access)) {
            if ($Rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                (($Rule.PropagationFlags -band
                    [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0)) {
                continue
            }
            try {
                $Sid = $Rule.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]
                ).Value
            }
            catch {
                throw "Portable first-contact source parent ACL has an unreadable principal."
            }
            if ($Sid -notin $TrustedSids -and
                -not ($ProtectedInstallerVolumeRoot -and $Sid -ceq $TrustedInstallerSid) -and
                (($Rule.FileSystemRights -band $DangerousRights) -ne 0)) {
                throw "Portable first-contact source parent is broadly writable. Extract into an administrator-owned private local NTFS directory."
            }
        }
        $ImmediateParent = $false
        $Current = [IO.Directory]::GetParent($Path)
    }
}

function Protect-PortableFirstContactData {
    $SourcePath = [IO.Path]::GetFullPath($PortableRoot)
    if ($SourcePath.StartsWith('\\', [StringComparison]::Ordinal)) {
        throw "Portable first contact cannot run from a UNC or remote source path."
    }
    Assert-PortableSourceParentChain
    $DriveRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($DataDir))
    try {
        $Drive = [IO.DriveInfo]::new($DriveRoot)
        $DriveType = $Drive.DriveType
        $DriveFormat = $Drive.DriveFormat
    }
    catch {
        throw "Portable first contact requires a local fixed NTFS volume with Windows ACL support."
    }
    if ($DriveType -ne [IO.DriveType]::Fixed -or $DriveFormat -cne "NTFS") {
        throw "Portable first contact requires a local fixed NTFS volume with Windows ACL support."
    }
    $Before = @(Get-PortablePlainPaths $PortableRoot "Portable first-contact source" | Sort-Object)
    $Icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    if (-not (Test-Path -LiteralPath $Icacls -PathType Leaf)) {
        throw "Windows ACL utility is unavailable."
    }
    foreach ($Boundary in @($PortableRoot, $DataDir)) {
        & $Icacls $Boundary "/setowner" "*S-1-5-32-544" "/C" "/Q" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not take ownership of a portable first-contact boundary."
        }
    }
    # The program directory remains readable for a later elevated retry; only
    # Administrators/SYSTEM can alter it. Data permits no ordinary-user read.
    Set-Acl -LiteralPath $PortableRoot -AclObject (New-PortableBoundaryAcl $true)
    Set-Acl -LiteralPath $DataDir -AclObject (New-PortableBoundaryAcl $false)
    foreach ($Child in @([IO.Directory]::EnumerateFileSystemEntries($PortableRoot))) {
        if ([string]::Equals($Child, $DataDir, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $Attributes = [IO.File]::GetAttributes($Child)
        if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Portable first-contact source contains a link or junction."
        }
        foreach ($Arguments in @(
            @($Child, "/setowner", "*S-1-5-32-544", "/C", "/Q"),
            @($Child, "/inheritance:e", "/C", "/Q"),
            @($Child, "/reset", "/C", "/Q")
        )) {
            & $Icacls @Arguments | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Could not lock a portable first-contact program file."
            }
        }
        if (($Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
            Repair-PortableAclChildren $Child $Icacls
        }
    }
    Repair-PortableAclChildren $DataDir $Icacls
    $After = @(Get-PortablePlainPaths $PortableRoot "Portable first-contact source" | Sort-Object)
    if (@(Compare-Object $Before $After).Count -ne 0) {
        throw "Portable first-contact source changed during ACL protection."
    }
    $DataPrefix = $DataDir.TrimEnd('\') + '\'
    foreach ($Path in $After) {
        if ([string]::Equals($Path, $DataDir, [StringComparison]::OrdinalIgnoreCase) -or
            $Path.StartsWith($DataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Assert-PortablePrivateAcl $Path ([string]::Equals(
                $Path, $DataDir, [StringComparison]::OrdinalIgnoreCase
            ))
        }
        else {
            Assert-PortableProtectedAcl $Path ([string]::Equals(
                $Path, $PortableRoot, [StringComparison]::OrdinalIgnoreCase
            ))
        }
    }
}

function Assert-PortableFirstContactDataPrivate {
    foreach ($Path in @(Get-PortablePlainPaths $DataDir "Portable first-contact data")) {
        Assert-PortablePrivateAcl $Path ([string]::Equals(
            $Path, $DataDir, [StringComparison]::OrdinalIgnoreCase
        ))
    }
}

function Protect-PortableUpdateRoot {
    $ProtectedRoot = [IO.Path]::GetFullPath(
        (Join-Path (Get-TrustedProgramDataRoot) "RigPilotAgentPortable")
    ).TrimEnd('\')
    if (-not [string]::Equals(
        $PortableRoot.TrimEnd('\'),
        $ProtectedRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "SYSTEM autostart and remote updates require this portable folder: $ProtectedRoot"
    }
    $LauncherPath = Join-Path $PortableRoot "portable-agent.ps1"
    $UpdaterPath = Join-Path $PortableRoot "RigPilotAgentUpdater.ps1"
    foreach ($RequiredPath in @($LauncherPath, $UpdaterPath)) {
        if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
            throw "A required portable update component is missing."
        }
        Assert-NotReparsePoint $RequiredPath "Portable update component"
    }
    $ReadyPath = Join-Path $DataDir "agent-update-ready.json"

    $Icacls = Join-Path $env:SystemRoot "System32\icacls.exe"
    if (-not (Test-Path -LiteralPath $Icacls -PathType Leaf)) {
        throw "Windows ACL utility is unavailable."
    }
    # Take ownership of the two already-validated plain directory boundaries;
    # never use recursive icacls over an unchecked tree.
    foreach ($BoundaryPath in @($PortableRoot, $DataDir)) {
        $Arguments = @($BoundaryPath, "/setowner", "*S-1-5-32-544", "/C", "/Q")
        & $Icacls @Arguments | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not take ownership of a portable boundary (icacls exit $LASTEXITCODE)."
        }
    }

    # Set both security boundaries atomically before repairing descendants.
    # The data subtree therefore never temporarily inherits Users read access.
    Set-Acl -LiteralPath $PortableRoot -AclObject (New-PortableBoundaryAcl $true)
    Set-Acl -LiteralPath $DataDir -AclObject (New-PortableBoundaryAcl $false)

    # Repair top-down without /T: each child is checked for reparse metadata,
    # secured from its already-trusted parent, then (for directories) entered.
    foreach ($RootChild in @([IO.Directory]::EnumerateFileSystemEntries($PortableRoot))) {
        if ([string]::Equals($RootChild, $DataDir, [StringComparison]::OrdinalIgnoreCase)) {
            continue
        }
        $RootChildAttributes = [IO.File]::GetAttributes($RootChild)
        if (($RootChildAttributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Portable update tree must not contain links or junctions: $RootChild"
        }
        foreach ($Arguments in @(
            @($RootChild, "/setowner", "*S-1-5-32-544", "/C", "/Q"),
            @($RootChild, "/inheritance:e", "/C", "/Q"),
            @($RootChild, "/reset", "/C", "/Q")
        )) {
            & $Icacls @Arguments | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Could not repair a portable root child ACL (icacls exit $LASTEXITCODE)."
            }
        }
        if (($RootChildAttributes -band [IO.FileAttributes]::Directory) -ne 0) {
            Repair-PortableAclChildren $RootChild $Icacls
        }
    }
    Repair-PortableAclChildren $DataDir $Icacls
    # WSL runs in the signed-in Windows user's security context, while package
    # download and verification remain SYSTEM-only.  Grant Users read/execute
    # on only the immutable package cache (never config/state/runtime); this
    # lets DrvFS execute verified Linux binaries without exposing a writable
    # path that could replace them between validation and launch.
    $PackagesPath = Join-Path $DataDir "packages"
    if (Test-Path -LiteralPath $PackagesPath -PathType Container) {
        Set-Acl -LiteralPath $PackagesPath -AclObject (New-PortableBoundaryAcl $true)
        Repair-PortableAclChildren $PackagesPath $Icacls
    }
    foreach ($TrustedFile in @($LauncherPath, $UpdaterPath)) {
        $Arguments = @($TrustedFile, "/inheritance:d", "/C", "/Q")
        & $Icacls @Arguments | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not lock a trusted portable script ACL (icacls exit $LASTEXITCODE)."
        }
    }

    foreach ($ProtectedPath in @($PortableRoot, $DataDir, $LauncherPath, $UpdaterPath)) {
        Assert-PortableProtectedAcl $ProtectedPath
    }
    if (Test-Path -LiteralPath $PackagesPath -PathType Container) {
        Assert-PortableProtectedAcl $PackagesPath
    }

    # Capability must remain disabled until the SYSTEM task is registered and
    # independently verified by Install-PortableAutostart.
    Remove-Item -LiteralPath $ReadyPath -Force -ErrorAction SilentlyContinue
}

function Invoke-PortableMigration {
    $MigrationScript = Join-Path $PortableRoot "portable-migrate.ps1"
    if (-not (Test-Path -LiteralPath $MigrationScript -PathType Leaf)) {
        throw "The protected portable migration component is missing."
    }
    Assert-NotReparsePoint $MigrationScript "Portable migration component"
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        $MigrationScript + '" -SourceRoot "' + $PortableRoot + '"'
    # -NoNewWindow keeps the coordinator's console output in this interactive
    # elevated console; a detached console closes on exit and used to hide the
    # actual preflight failure behind a bare exit code.
    $Migration = Start-Process `
        -FilePath $PowerShellPath `
        -ArgumentList $Arguments `
        -WorkingDirectory $PortableRoot `
        -NoNewWindow `
        -Wait `
        -PassThru
    $Migration.Refresh()
    if ([int]$Migration.ExitCode -ne 0) {
        $MigrationLogPath = Join-Path (Join-Path $DataDir "logs") "portable-migration.log"
        $Tail = Get-PortableLogTail $MigrationLogPath 12
        $Detail = if ($Tail) {
            " Last migration log: $Tail (full log: $MigrationLogPath)"
        }
        else {
            ""
        }
        throw "Portable migration failed (exit $($Migration.ExitCode)). The old folder was kept.$Detail"
    }
}

function Write-PortableUpdateReadyMarker {
    $UpdaterPath = Join-Path $PortableRoot "RigPilotAgentUpdater.ps1"
    $ReadyPath = Join-Path $DataDir "agent-update-ready.json"
    $Ready = [ordered]@{
        schema_version = 1
        acl_protected = $true
        updater_sha256 = (Get-FileHash -LiteralPath $UpdaterPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $TemporaryReady = Join-Path $DataDir (".agent-update-ready." + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        [IO.File]::WriteAllText(
            $TemporaryReady,
            (($Ready | ConvertTo-Json -Compress) + [Environment]::NewLine),
            $Utf8NoBom
        )
        Install-TemporaryFile $TemporaryReady $ReadyPath
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryReady) {
            Remove-Item -LiteralPath $TemporaryReady -Force
        }
    }
}

function Test-PortableEnrolledIdentityFile {
    # Mirror the migration coordinator's Read-EnrollmentIdentity criteria so
    # Install-Autostart explains an unusable identity itself instead of losing
    # the reason inside the migration child's console.
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return $false
    }
    $Item = Get-Item -LiteralPath $StatePath -Force
    if ($Item.PSIsContainer -or $Item.Length -gt 4MB) {
        return $false
    }
    try {
        $Document = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        return $false
    }
    if ($null -eq $Document) {
        return $false
    }
    $AgentId = [string]$Document.agent_id
    $AgentSecret = [string]$Document.agent_secret
    if ($AgentId -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$' -or
        [string]::IsNullOrWhiteSpace($AgentSecret) -or
        $AgentSecret.Length -gt 2048) {
        return $false
    }
    return $true
}

function Get-PortableLogTail([string]$Path, [int]$LineCount) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return ""
    }
    try {
        $Text = [IO.File]::ReadAllText($Path)
    }
    catch {
        return ""
    }
    $Entries = @($Text -split "\r?\n" |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($Entries.Count -eq 0) {
        return ""
    }
    $First = [Math]::Max(0, $Entries.Count - $LineCount)
    return (@($Entries[$First..($Entries.Count - 1)]) -join " | ")
}

function Write-PortableLauncherFailureLog([string]$Message) {
    # Durable, bounded, secret-free failure trail.  The interactive installer
    # console closes when the elevated process exits; this log lets support and
    # the migration coordinator recover the exact failure reason afterwards.
    try {
        $LogDir = Join-Path $DataDir "logs"
        [IO.Directory]::CreateDirectory($LogDir) | Out-Null
        $LogPath = Join-Path $LogDir "portable-launcher.log"
        $Line = ([DateTime]::UtcNow.ToString("o")) +
            " action=" + $Action + " " + $Message
        $Existing = ""
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) {
            $Existing = [IO.File]::ReadAllText($LogPath)
        }
        $Combined = $Existing + $Line + [Environment]::NewLine
        if ($Combined.Length -gt 1048576) {
            $Combined = $Combined.Substring($Combined.Length - 65536)
        }
        [IO.File]::WriteAllText($LogPath, $Combined)
    }
    catch {
        # Failure logging must never mask the original launcher error.
    }
}

function Assert-PortableEnrolledIdentityReady {
    # Single preflight used by both Install-PortableAutostart branches: an
    # unreadable or incomplete identity must fail with an actionable message
    # before migration or a SYSTEM task registration is attempted.
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        throw "Run Start-RigPilotAgent.bat once and complete enrollment before installing autostart."
    }
    if (-not (Test-PortableEnrolledIdentityFile)) {
        throw "Portable device state has no valid enrollment identity. Complete enrollment via Start-RigPilotAgent.bat or restore a valid data\state.json before installing autostart."
    }
}

function Install-PortableAutostart {
    $ProtectedRoot = [IO.Path]::GetFullPath(
        (Join-Path (Get-TrustedProgramDataRoot) "RigPilotAgentPortable")
    ).TrimEnd('\')
    if (-not [string]::Equals(
        $PortableRoot.TrimEnd('\'),
        $ProtectedRoot,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        Assert-PortableEnrolledIdentityReady
        Invoke-PortableMigration
        return
    }
    Assert-PortableEnrolledIdentityReady
    Assert-NotReparsePoint $DataDir "Portable data directory"
    Assert-NotReparsePoint $ConfigPath "Portable config"
    Assert-NotReparsePoint $StatePath "Portable device state"
    Import-Module ScheduledTasks -ErrorAction Stop
    $ExistingTask = Get-ScheduledTask -TaskName $AutostartTaskName `
        -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -ne $ExistingTask -and -not (Test-OwnedAutostartTask $ExistingTask)) {
        throw "Scheduled task name collision: $AutostartTaskName"
    }

    # A task-name collision is a pure preflight failure.  Check it before ACL
    # repair or ready-marker removal so a conflicting task causes zero local
    # mutation.
    Protect-PortableUpdateRoot
    if ($ActiveAgentPointerRepairNeeded) {
        $BrokenPointer = Join-Path $DataDir "active-agent.json"
        Assert-NotReparsePoint $BrokenPointer "Invalid active Agent pointer"
        Remove-Item -LiteralPath $BrokenPointer -Force
        Write-Host "Repaired an incomplete Agent activation: $ActiveAgentPointerFailure" `
            -ForegroundColor Yellow
    }
    # Install-Autostart is the single supported setup action.  Existing
    # portable configs may have retained explicit false values from an older
    # release, so enable both power actions atomically before the task starts.
    Set-PortablePowerActions -RequireConfig $true | Out-Null

    $ScriptPath = [IO.Path]::GetFullPath($script:PSCommandPath)
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $TaskArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $ScriptPath + '" -Action Start -Scheduled'
    $TaskAction = New-ScheduledTaskAction `
        -Execute $PowerShellPath `
        -Argument $TaskArguments `
        -WorkingDirectory $PortableRoot
    $TaskTrigger = New-ScheduledTaskTrigger -AtStartup
    $TaskPrincipal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest
    $TaskSettings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew
    # Use the scheduler's canonical unlimited duration directly. Passing
    # TimeSpan.Zero through the cmdlet may serialize an out-of-range value;
    # omitting the setting instead stops boot-triggered runs after 72 hours.
    # The update-recovery task does not supervise ordinary Agent runs.
    $TaskSettings.ExecutionTimeLimit = "PT0S"
    $Task = New-ScheduledTask `
        -Action $TaskAction `
        -Trigger $TaskTrigger `
        -Principal $TaskPrincipal `
        -Settings $TaskSettings `
        -Description "Start this RigPilot portable Agent folder when Windows starts."
    Register-ScheduledTask -TaskName $AutostartTaskName -TaskPath "\" `
        -InputObject $Task -Force | Out-Null

    $RegisteredTask = Get-ScheduledTask -TaskName $AutostartTaskName `
        -TaskPath "\" -ErrorAction Stop
    if (-not (Test-OwnedAutostartTask $RegisteredTask)) {
        throw "The autostart task was registered with unexpected arguments."
    }
    # The bounded update-recovery watchdog is part of the same setup
    # transaction: fail the install while update capability is still
    # disabled, so a rig never advertises updates without anchor-level
    # recovery.
    Install-PortableRecoveryWatchdog
    # Start the verified background task immediately; the startup trigger keeps
    # it persistent across future reboots.
    Start-ScheduledTask -TaskName $AutostartTaskName -TaskPath "\"
    Write-PortableUpdateReadyMarker
    Write-Host "Autostart installed: $AutostartTaskName" -ForegroundColor Green
    Write-Host "Update recovery task installed: $RecoveryTaskName"
    Write-Host "The background Agent is starting now and will start again at Windows boot."
}

function Remove-PortableAutostart {
    Import-Module ScheduledTasks -ErrorAction Stop
    $ExistingTask = Get-ScheduledTask -TaskName $AutostartTaskName `
        -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -eq $ExistingTask) {
        Remove-PortableRecoveryWatchdog
        Remove-Item -LiteralPath (Join-Path $DataDir "agent-update-ready.json") `
            -Force -ErrorAction SilentlyContinue
        Write-Host "Autostart is not installed for this portable folder." -ForegroundColor Yellow
        return
    }
    if (-not (Test-OwnedAutostartTask $ExistingTask)) {
        throw "Refusing to remove an unexpected scheduled task named $AutostartTaskName."
    }
    Unregister-ScheduledTask -TaskName $AutostartTaskName `
        -TaskPath "\" -Confirm:$false
    Remove-PortableRecoveryWatchdog
    Remove-Item -LiteralPath (Join-Path $DataDir "agent-update-ready.json") `
        -Force -ErrorAction SilentlyContinue
    Write-Host "Autostart removed: $AutostartTaskName" -ForegroundColor Green
}

function Remove-PortableRecoveryWatchdog {
    Import-Module ScheduledTasks -ErrorAction Stop
    $ExistingTask = Get-ScheduledTask -TaskName $RecoveryTaskName `
        -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -eq $ExistingTask) {
        return
    }
    if (-not (Test-OwnedRecoveryTask $ExistingTask)) {
        throw "Refusing to remove an unexpected scheduled task named $RecoveryTaskName."
    }
    Unregister-ScheduledTask -TaskName $RecoveryTaskName `
        -TaskPath "\" -Confirm:$false
    Write-Host "Update recovery task removed: $RecoveryTaskName" -ForegroundColor Green
}

function Test-ControllerUrl([string]$Value) {
    [Uri]$Uri = $null
    if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$Uri)) {
        return $false
    }
    if ($Uri.UserInfo -or $Uri.Query -or $Uri.Fragment) {
        return $false
    }
    if ($Uri.Scheme -eq "https") {
        return $true
    }
    return $Uri.Scheme -eq "http" -and $Uri.Host.ToLowerInvariant() -in @(
        "127.0.0.1", "localhost", "::1"
    )
}

function Test-PortableControllerConnection {
    $DefaultController = "https://controller.example.invalid"
    $Controller = [Environment]::GetEnvironmentVariable("RIGPILOT_CONTROLLER_URL")
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        try {
            $ExistingConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if (-not [string]::IsNullOrWhiteSpace([string]$ExistingConfig.server_url)) {
                $Controller = ([string]$ExistingConfig.server_url).TrimEnd('/')
            }
        }
        catch {
            Write-Host "Config: FAILED - $($_.Exception.Message)" -ForegroundColor Red
            return $false
        }
    }
    else {
        if ([string]::IsNullOrWhiteSpace($Controller)) {
            Write-Host "Set RIGPILOT_CONTROLLER_URL to your HTTPS controller address (example: $DefaultController)." -ForegroundColor Yellow
            return $false
        }
    }
    if (-not (Test-ControllerUrl $Controller)) {
        Write-Host "Controller URL: FAILED - HTTPS is required except for localhost." -ForegroundColor Red
        return $false
    }

    [Uri]$ControllerUri = $Controller
    $HostName = $ControllerUri.DnsSafeHost
    $Port = $ControllerUri.Port
    $AllChecksPassed = $true
    Write-Host "Controller: $Controller"
    Write-Host "TLS certificate validation is enabled; this check never falls back to a bare IP or ignores certificate errors."

    try {
        $DnsAddresses = @()
        if ($null -ne (Get-Command -Name Resolve-DnsName -ErrorAction SilentlyContinue)) {
            $DnsRecords = @(Resolve-DnsName `
                -Name $HostName `
                -DnsOnly `
                -QuickTimeout `
                -ErrorAction Stop)
            $DnsAddresses = @($DnsRecords | ForEach-Object {
                if (-not [string]::IsNullOrWhiteSpace([string]$_.IPAddress)) {
                    [string]$_.IPAddress
                }
            } | Select-Object -Unique)
        }
        else {
            $DnsAddresses = @([Net.Dns]::GetHostAddresses($HostName) | ForEach-Object {
                $_.IPAddressToString
            })
        }
        if ($DnsAddresses.Count -eq 0) {
            throw "DNS returned no IP addresses."
        }
        Write-Host ("DNS: OK - " + ($DnsAddresses -join ", ")) -ForegroundColor Green
    }
    catch {
        Write-Host "DNS: FAILED - $($_.Exception.Message)" -ForegroundColor Red
        $AllChecksPassed = $false
    }

    $TcpClient = New-Object Net.Sockets.TcpClient
    $AsyncResult = $null
    try {
        $AsyncResult = $TcpClient.BeginConnect($HostName, $Port, $null, $null)
        if (-not $AsyncResult.AsyncWaitHandle.WaitOne(3000, $false)) {
            throw "connection timed out after 3 seconds"
        }
        $TcpClient.EndConnect($AsyncResult)
        Write-Host "TCP ${HostName}:${Port}: OK" -ForegroundColor Green
    }
    catch {
        Write-Host "TCP ${HostName}:${Port}: FAILED - $($_.Exception.Message)" -ForegroundColor Red
        $AllChecksPassed = $false
    }
    finally {
        if ($null -ne $AsyncResult) {
            $AsyncResult.AsyncWaitHandle.Close()
        }
        $TcpClient.Close()
    }

    $HealthUrl = $Controller.TrimEnd('/') + "/api/health"
    try {
        # Windows 10's .NET defaults can omit TLS 1.2.  Add it without
        # disabling the OS certificate validator or any existing protocol.
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $Response = Invoke-WebRequest `
            -Uri $HealthUrl `
            -UseBasicParsing `
            -TimeoutSec 6 `
            -ErrorAction Stop
        $Health = $Response.Content | ConvertFrom-Json
        $VersionText = ""
        if (-not [string]::IsNullOrWhiteSpace([string]$Health.version)) {
            $VersionText = " (version " + [string]$Health.version + ")"
        }
        Write-Host "HTTPS /api/health: OK - HTTP $([int]$Response.StatusCode)$VersionText" -ForegroundColor Green
    }
    catch {
        Write-Host "HTTPS /api/health: FAILED - $($_.Exception.Message)" -ForegroundColor Red
        $AllChecksPassed = $false
    }
    return $AllChecksPassed
}

function Read-PlainSecret([string]$Prompt) {
    $SecureValue = Read-Host $Prompt -AsSecureString
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

function Get-OwnedAgentProcesses {
    $Expected = [IO.Path]::GetFullPath($AgentExe)
    $Candidates = @(Get-WindowsProcessInfo `
        -Filter "Name='RigPilotAgent.exe'" `
        -FallbackName "RigPilotAgent")
    foreach ($Candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace([string]$Candidate.ExecutablePath)) {
            continue
        }
        try {
            $Actual = [IO.Path]::GetFullPath([string]$Candidate.ExecutablePath)
        }
        catch {
            continue
        }
        if ([string]::Equals($Actual, $Expected, [StringComparison]::OrdinalIgnoreCase)) {
            $Candidate
        }
    }
}

function Test-PortableProtectedAgentRunning {
    # A failed source preflight must not start a second copy while an already
    # installed protected Agent (including a version slot) is running. Do not
    # inspect or replace another installation's identity to justify fallback.
    $ProtectedRoot = [IO.Path]::GetFullPath(
        (Join-Path (Get-TrustedProgramDataRoot) "RigPilotAgentPortable")
    ).TrimEnd('\')
    $Base = Join-Path $ProtectedRoot "RigPilotAgent.exe"
    $VersionsPrefix = (Join-Path $ProtectedRoot "versions").TrimEnd('\') + '\'
    foreach ($Candidate in @(Get-WindowsProcessInfo -Filter "Name='RigPilotAgent.exe'" -FallbackName "RigPilotAgent")) {
        if ([string]::IsNullOrWhiteSpace([string]$Candidate.ExecutablePath)) { continue }
        try { $Actual = [IO.Path]::GetFullPath([string]$Candidate.ExecutablePath) } catch { continue }
        if ([string]::Equals($Actual, $Base, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        if ($Actual.StartsWith($VersionsPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $Parts = $Actual.Substring($VersionsPrefix.Length).Split('\')
            if ($Parts.Count -eq 2 -and
                $Parts[0] -match '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -and
                [string]::Equals($Parts[1], "RigPilotAgent.exe", [StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
    }
    return $false
}

function Write-PortableConfig([bool]$Unattended) {
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        throw "state.json exists but config.json is missing. Restore both files or remove both before re-enrolling."
    }

    if ($Unattended) {
        $Controller = [Environment]::GetEnvironmentVariable(
            "RIGPILOT_CONTROLLER_URL", [EnvironmentVariableTarget]::Process
        )
        $DeviceName = [Environment]::GetEnvironmentVariable(
            "RIGPILOT_DEVICE_NAME", [EnvironmentVariableTarget]::Process
        )
        if ([string]::IsNullOrWhiteSpace($Controller) -or
            [string]::IsNullOrWhiteSpace($DeviceName)) {
            throw "Unattended first contact requires a controller URL and device name."
        }
        $Controller = $Controller.Trim()
        $DeviceName = $DeviceName.Trim()
    }
    else {
        $DefaultController = "https://controller.example.invalid"
        $Controller = (Read-Host "Controller URL [example: $DefaultController]").Trim()
        if (-not $Controller -or $Controller -eq $DefaultController) {
            throw "Enter your Controller HTTPS URL."
        }
        $DefaultName = $env:COMPUTERNAME
        $DeviceName = (Read-Host "Device name [$DefaultName]").Trim()
        if (-not $DeviceName) {
            $DeviceName = $DefaultName
        }
    }
    if (-not (Test-ControllerUrl $Controller)) {
        throw "Controller URL must use HTTPS; HTTP is allowed only for localhost."
    }
    $Controller = $Controller.TrimEnd('/')
    if ($DeviceName.Length -lt 1 -or $DeviceName.Length -gt 80) {
        throw "Device name must contain 1-80 characters."
    }

    $Config = [ordered]@{
        server_url = $Controller
        state_file = "state.json"
        name = $DeviceName
        heartbeat_interval = 30
        request_timeout = 20
        log_level = "INFO"
        log_file = "logs\agent.jsonl"
        dry_run = $false
        service_allowlist = @()
        allow_reboot = $true
        allow_shutdown = $true
        allow_agent_updates = $true
        agent_update_max_mb = 512
        agent_update_total_timeout = 900
        collect_gpu_metrics = $true
        nvidia_smi_path = "nvidia-smi"
        flight_sheet_file = "flight-sheet.json"
        miner_status_file = "miner-status.json"
        managed_mining_enabled = $true
        managed_miner_runtime_dir = "miner-runtime"
        miner_profile_allowlist = @("default")
        flight_sheet_extra_key_allowlist = @()
        miner_service = $null
        allow_miner_downloads = $true
        miner_packages_dir = "packages"
        miner_download_host_allowlist = @()
        miner_download_max_mb = 512
        miner_download_total_timeout = 300
        allow_gpu_oc = $true
        gpu_oc_allowed_vendors = @("nvidia")
        command_history_limit = 200
    }
    $Json = $Config | ConvertTo-Json -Depth 6
    $TemporaryConfig = Join-Path $DataDir (".config." + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        [IO.File]::WriteAllText($TemporaryConfig, $Json + [Environment]::NewLine, $Utf8NoBom)
        [IO.File]::Move($TemporaryConfig, $ConfigPath)
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryConfig) {
            Remove-Item -LiteralPath $TemporaryConfig -Force
        }
    }
}

function Update-PortableConfig {
    # A previous portable folder may be upgraded in place. Add new settings
    # only when absent, preserve explicit opt-outs, and retire obsolete keys.
    $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Changed = $false
    $Defaults = [ordered]@{
        managed_mining_enabled = $true
        managed_miner_runtime_dir = "miner-runtime"
        allow_agent_updates = $true
        agent_update_max_mb = 512
        agent_update_total_timeout = 900
    }
    foreach ($Default in $Defaults.GetEnumerator()) {
        if ($null -eq $Config.PSObject.Properties[$Default.Key]) {
            $Config | Add-Member -NotePropertyName $Default.Key -NotePropertyValue $Default.Value
            $Changed = $true
        }
    }
    # Older installations may still carry the retired Wi-Fi management key.
    # Remove it when the portable launcher runs; binary-only updates ignore it.
    if ($null -ne $Config.PSObject.Properties["allow_wifi_management"]) {
        $Config.PSObject.Properties.Remove("allow_wifi_management")
        $Changed = $true
    }
    if (-not $Changed) {
        return
    }
    $TemporaryConfig = Join-Path $DataDir (".config.upgrade." + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        $Json = $Config | ConvertTo-Json -Depth 8
        [IO.File]::WriteAllText($TemporaryConfig, $Json + [Environment]::NewLine, $Utf8NoBom)
        Install-TemporaryFile $TemporaryConfig $ConfigPath
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryConfig) {
            Remove-Item -LiteralPath $TemporaryConfig -Force
        }
    }
}

function Set-PortablePowerActions([bool]$RequireConfig) {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        if ($RequireConfig) {
            throw "Portable config is missing; complete enrollment before installing autostart."
        }
        return $false
    }
    Assert-NotReparsePoint $DataDir "Portable data directory"
    Assert-NotReparsePoint $ConfigPath "Portable config"

    $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Changed = $false
    foreach ($PropertyName in @("allow_reboot", "allow_shutdown")) {
        $Existing = $Config.PSObject.Properties[$PropertyName]
        if ($null -ne $Existing -and $Existing.Value -is [bool] -and
            [bool]$Existing.Value) {
            continue
        }
        # Assigning PSPropertyInfo.Value is not reliable on all Windows
        # PowerShell 5.1 builds. Add-Member -Force safely creates or replaces
        # the note property without disturbing any other config field.
        $Config | Add-Member `
            -NotePropertyName $PropertyName `
            -NotePropertyValue $true `
            -Force
        $Changed = $true
    }
    if (-not $Changed) {
        return $false
    }

    $TemporaryConfig = Join-Path $DataDir (".config.power." + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        $Json = $Config | ConvertTo-Json -Depth 8
        [IO.File]::WriteAllText($TemporaryConfig, $Json + [Environment]::NewLine, $Utf8NoBom)
        Install-TemporaryFile $TemporaryConfig $ConfigPath
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryConfig) {
            Remove-Item -LiteralPath $TemporaryConfig -Force
        }
    }
    return $true
}

function Test-PortablePending([string]$Kind) {
    if ($Kind -notin @("enrollment", "replacement")) {
        throw "Unknown portable first-contact pending type."
    }
    $PendingPath = Join-Path $DataDir (
        "." + [IO.Path]::GetFileName($StatePath) + ".$Kind-pending"
    )
    # Enumerate names without following the entry. A broken link, directory or
    # damaged journal must still reach the Agent's fail-closed validation.
    foreach ($Entry in [IO.Directory]::EnumerateFileSystemEntries($DataDir)) {
        if ([string]::Equals($Entry, $PendingPath, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Test-PortableEnrollmentPending {
    return (Test-PortablePending "enrollment")
}

function Test-PortableReplacementPending {
    return (Test-PortablePending "replacement")
}

function Assert-PortableFirstContactTargetAvailable {
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        return
    }
    $ProtectedRoot = [IO.Path]::GetFullPath(
        (Join-Path (Get-TrustedProgramDataRoot) "RigPilotAgentPortable")
    ).TrimEnd('\')
    $Parent = Split-Path -Parent $ProtectedRoot
    if (Test-Path -LiteralPath $Parent -PathType Container) {
        # Enumeration detects even a dangling link at the protected path.
        foreach ($Entry in [IO.Directory]::EnumerateFileSystemEntries($Parent)) {
            if ([string]::Equals($Entry, $ProtectedRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "A protected RigPilot Agent installation already exists. Preserve its identity and use its existing installation; first contact cannot replace its data."
            }
        }
    }
}

function Invoke-PortableEnrollmentAttempt(
    [ValidateSet("Enroll", "Inherit")][string]$Mode,
    [string]$Token,
    [ref]$ExitCode
) {
    $ExitCode.Value = -1
    try {
        # Pass exactly one short-lived code to this --once child process.
        [Environment]::SetEnvironmentVariable(
            "RIGPILOT_ENROLLMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            "RIGPILOT_REPLACEMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
        )
        if (-not [string]::IsNullOrEmpty($Token)) {
            $Variable = if ($Mode -eq "Inherit") {
                "RIGPILOT_REPLACEMENT_TOKEN"
            }
            else {
                "RIGPILOT_ENROLLMENT_TOKEN"
            }
            [Environment]::SetEnvironmentVariable(
                $Variable, $Token, [EnvironmentVariableTarget]::Process
            )
        }
        & $AgentExe --config $ConfigPath --once
        $ExitCode.Value = $LASTEXITCODE
    }
    finally {
        [Environment]::SetEnvironmentVariable(
            "RIGPILOT_ENROLLMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            "RIGPILOT_REPLACEMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
        )
        $Token = $null
        # Keep newly written state, pending material, and logs private in the
        # old source folder that remains after a successful migration.
        Protect-PortableFirstContactData
    }
}

function Invoke-Enrollment([string]$ProvidedToken) {
    $Token = if ([string]::IsNullOrEmpty($ProvidedToken)) {
        Read-PlainSecret "One-time enrollment token"
    }
    else {
        $ProvidedToken
    }
    if ([string]::IsNullOrWhiteSpace($Token) -or $Token.Trim().Length -lt 20 -or $Token.Trim().Length -gt 256) {
        throw "Enrollment token length is invalid."
    }
    $EnrollmentExitCode = -1
    try {
        Invoke-PortableEnrollmentAttempt "Enroll" $Token ([ref]$EnrollmentExitCode)
    }
    finally {
        $Token = $null
    }
    if ($EnrollmentExitCode -ne 0 -or -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        throw "Enrollment failed. Verify the controller URL and one-time token, then run Start again."
    }
}

function Invoke-Inheritance([string]$ProvidedToken) {
    $Token = if ([string]::IsNullOrEmpty($ProvidedToken)) {
        Read-PlainSecret "One-time device inheritance code"
    }
    else {
        $ProvidedToken
    }
    if ($Token -cnotmatch '^rpr_[A-Za-z0-9_-]{43}$') {
        throw "Device inheritance code format is invalid."
    }
    $InheritanceExitCode = -1
    try {
        Invoke-PortableEnrollmentAttempt "Inherit" $Token ([ref]$InheritanceExitCode)
    }
    finally {
        $Token = $null
    }
    if ($InheritanceExitCode -ne 0 -or -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        throw "Device inheritance failed. Keep the current data directory and retry only the original code."
    }
}

function Invoke-EnrollmentRecovery([bool]$ScheduledMode, [string]$ProvidedToken) {
    $RecoveryExitCode = -1
    Invoke-PortableEnrollmentAttempt "Enroll" $null ([ref]$RecoveryExitCode)
    if ($RecoveryExitCode -eq 0 -and (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return
    }
    if ((Test-Path -LiteralPath $StatePath -PathType Leaf) -or
        $RecoveryExitCode -eq 0 -or -not (Test-PortableEnrollmentPending)) {
        throw "Enrollment recovery did not complete. Correct the reported problem and run Start again."
    }
    if ($ScheduledMode -and [string]::IsNullOrEmpty($ProvidedToken)) {
        throw "Autostart could not recover the pending enrollment. Run Start interactively to retry the original token."
    }
    $Token = if ([string]::IsNullOrEmpty($ProvidedToken)) {
        Read-PlainSecret "Original enrollment token (same token only; leave blank to stop)"
    }
    else {
        $ProvidedToken
    }
    if ([string]::IsNullOrWhiteSpace($Token)) {
        throw "Enrollment recovery is incomplete. The pending request has been retained."
    }
    if ($Token.Trim().Length -lt 20 -or $Token.Trim().Length -gt 256) {
        throw "Original enrollment token length is invalid; the pending request has been retained."
    }
    try {
        # The Agent verifies the original token hash and reuses the journal.
        # Only its exact not-created response may clear a rejected request.
        Invoke-PortableEnrollmentAttempt "Enroll" $Token ([ref]$RecoveryExitCode)
    }
    finally {
        $Token = $null
    }
    if ($RecoveryExitCode -ne 0 -or -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        throw "Enrollment recovery failed. Correct the reported problem and run Start again."
    }
}

function Invoke-InheritanceRecovery([bool]$ScheduledMode, [string]$ProvidedToken) {
    if ($ScheduledMode -and [string]::IsNullOrEmpty($ProvidedToken)) {
        throw "Autostart cannot recover pending device inheritance without the original code."
    }
    $Token = if ([string]::IsNullOrEmpty($ProvidedToken)) {
        Read-PlainSecret "Original device inheritance code (same code only; leave blank to stop)"
    }
    else {
        $ProvidedToken
    }
    if ($Token -cnotmatch '^rpr_[A-Za-z0-9_-]{43}$') {
        throw "Original device inheritance code format is invalid; the pending request was retained."
    }
    $RecoveryExitCode = -1
    try {
        Invoke-PortableEnrollmentAttempt "Inherit" $Token ([ref]$RecoveryExitCode)
    }
    finally {
        $Token = $null
    }
    if ($RecoveryExitCode -ne 0 -or -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        throw "Device inheritance recovery failed. Keep the pending request and retry its original code."
    }
}

function Initialize-PortableEnrollment([bool]$ScheduledMode) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    Assert-NotReparsePoint $DataDir "Portable data directory"

    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        New-Item -ItemType Directory -Path (Join-Path $DataDir "logs") -Force | Out-Null
        New-Item -ItemType Directory -Path (Join-Path $DataDir "packages") -Force | Out-Null
        if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
            throw "state.json exists but config.json is missing. Restore both files before starting."
        }
        Update-PortableConfig
        [Environment]::SetEnvironmentVariable(
            "RIGPILOT_ENROLLMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
        )
        [Environment]::SetEnvironmentVariable(
            "RIGPILOT_REPLACEMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
        )
        return
    }
    # Capture the selected mode locally and remove both codes before ACL
    # protection can launch icacls. Only the enrollment Agent child may
    # receive a first-contact code through its process environment.
    $ExplicitEnrollment = [Environment]::GetEnvironmentVariable(
        "RIGPILOT_ENROLLMENT_TOKEN", [EnvironmentVariableTarget]::Process
    )
    $ExplicitReplacement = [Environment]::GetEnvironmentVariable(
        "RIGPILOT_REPLACEMENT_TOKEN", [EnvironmentVariableTarget]::Process
    )
    [Environment]::SetEnvironmentVariable(
        "RIGPILOT_ENROLLMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
    )
    [Environment]::SetEnvironmentVariable(
        "RIGPILOT_REPLACEMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
    )
    Assert-PortableFirstContactTargetAvailable
    Protect-PortableFirstContactData
    New-Item -ItemType Directory -Path (Join-Path $DataDir "logs") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $DataDir "packages") -Force | Out-Null
    $EnrollmentPending = Test-PortableEnrollmentPending
    $ReplacementPending = Test-PortableReplacementPending
    if ($EnrollmentPending -and $ReplacementPending) {
        throw "Both registration and device inheritance are pending. Preserve data and resolve the conflicting state before retrying."
    }
    if (($EnrollmentPending -or $ReplacementPending) -and
        -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "First-contact recovery requires the original config.json. Restore it with the pending state."
    }
    if (-not [string]::IsNullOrEmpty($ExplicitEnrollment) -and
        -not [string]::IsNullOrEmpty($ExplicitReplacement)) {
        throw "Normal enrollment and device inheritance codes cannot be supplied together."
    }
    if (($EnrollmentPending -and -not [string]::IsNullOrEmpty($ExplicitReplacement)) -or
        ($ReplacementPending -and -not [string]::IsNullOrEmpty($ExplicitEnrollment))) {
        throw "The supplied code type conflicts with the pending first-contact request."
    }

    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        $HasExplicitCode = (-not [string]::IsNullOrEmpty($ExplicitEnrollment)) -or (-not [string]::IsNullOrEmpty($ExplicitReplacement))
        if ($ScheduledMode -and -not $HasExplicitCode) {
            throw "Autostart cannot create an interactive config. Run Install-Autostart.bat once first."
        }
        Write-PortableConfig ([bool]$HasExplicitCode)
    }
    Update-PortableConfig
    # Newly created config/log/package files may have the elevated account as
    # owner even when they inherited a private ACE. Re-own and recheck them
    # before the Agent can submit either first-contact request.
    Protect-PortableFirstContactData
    Assert-PortableFirstContactDataPrivate
    if ($EnrollmentPending) {
        Invoke-EnrollmentRecovery $ScheduledMode $ExplicitEnrollment
        return
    }
    if ($ReplacementPending) {
        Invoke-InheritanceRecovery $ScheduledMode $ExplicitReplacement
        return
    }
    if (-not [string]::IsNullOrEmpty($ExplicitEnrollment)) {
        Invoke-Enrollment $ExplicitEnrollment
        return
    }
    if (-not [string]::IsNullOrEmpty($ExplicitReplacement)) {
        Invoke-Inheritance $ExplicitReplacement
        return
    }
    if ($ScheduledMode) {
        throw "Autostart cannot enroll or inherit a device. Run Install-Autostart.bat once first."
    }
    $Choice = (Read-Host "First contact: [1] New device, [2] Inherit existing device identity").Trim()
    if ($Choice -ceq "1") {
        Invoke-Enrollment $null
    }
    elseif ($Choice -ceq "2") {
        Invoke-Inheritance $null
    }
    else {
        throw "Choose 1 for a new device or 2 to inherit an existing device identity."
    }
}

function Cancel-PortableUpdateRecoveryIntent {
    # Called only by explicit elevated Stop/RemoveAutostart, under the same
    # launch mutex as recovery children. Never create a generic stop marker.
    $Jobs = Join-Path $DataDir "agent-updates\staged"
    if (-not (Test-Path -LiteralPath $Jobs -PathType Container)) { return }
    Assert-NotReparsePoint $Jobs "Update jobs"
    foreach ($Job in @(Get-ChildItem -LiteralPath $Jobs -Directory -Force)) {
        if ($Job.Name -notmatch '^[0-9a-f]{32}$') { continue }
        Assert-NotReparsePoint $Job.FullName "Update job"
        $MarkerPath = Join-Path $Job.FullName "recovery-restart.json"
        if (-not (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) { continue }
        Assert-NotReparsePoint $MarkerPath "Update recovery intent"
        if ((Get-Item -LiteralPath $MarkerPath).Length -gt 32768) { continue }
        try { $Intent = Get-Content -LiteralPath $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { continue }
        if ([string]$Intent.intent_kind -ne "bound-rollback-restart.v1" -or
            [string]$Intent.activation_id -cne $Job.Name -or
            [string]$Intent.phase -notin @("handoff", "rollback")) { continue }
        $Intent.phase = "cancelled"
        $Temporary = Join-Path $Job.FullName (".recovery-cancel-" + [Guid]::NewGuid().ToString("N") + ".tmp")
        $Backup = Join-Path $Job.FullName (".recovery-cancel-backup-" + [Guid]::NewGuid().ToString("N") + ".tmp")
        try {
            [IO.File]::WriteAllText($Temporary, ($Intent | ConvertTo-Json -Depth 8 -Compress), $Utf8NoBom)
            [IO.File]::Replace($Temporary, $MarkerPath, $Backup, $true)
        }
        finally {
            if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary -Force }
            if (Test-Path -LiteralPath $Backup) { Remove-Item -LiteralPath $Backup -Force }
        }
    }
}

function Test-PortableRecoveryLaunchAllowed([string]$Activation) {
    if ($Activation -notmatch '^[0-9a-f]{32}$') { return $false }
    $Marker = Join-Path $DataDir "agent-updates\staged\$Activation\recovery-restart.json"
    if (-not (Test-Path -LiteralPath $Marker -PathType Leaf)) { return $false }
    Assert-NotReparsePoint $Marker "Update recovery intent"
    if ((Get-Item -LiteralPath $Marker).Length -gt 32768) { return $false }
    $Intent = Get-Content -LiteralPath $Marker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$Intent.intent_kind -ne "bound-rollback-restart.v1" -or
        [string]$Intent.activation_id -cne $Activation -or
        [string]$Intent.phase -notin @("handoff", "rollback")) { return $false }
    $Relative = [string]$Intent.restore_relative
    $Version = [string]$Intent.restore_version
    $ExpectedHash = [string]$Intent.restore_sha256
    $ExpectedPointer = $Intent.restore_pointer
    if ([string]$Intent.phase -eq "handoff") {
        $Version = [string]$Intent.version
        $Relative = "versions/$Version/RigPilotAgent.exe"
        $ExpectedHash = [string]$Intent.candidate_sha256
        $ExpectedPointer = [pscustomobject]@{
            schema_version=1; version=$Version; relative_executable=$Relative
            activation_id=$Activation; command_id=[string]$Intent.command_id
        }
    }
    if ($Version -notmatch '^\d+\.\d+\.\d+$' -or
        $Relative -cnotin @("RigPilotAgent.exe", "versions/$Version/RigPilotAgent.exe")) { return $false }
    $CurrentPointer = $null
    $PointerFile = Join-Path $DataDir "active-agent.json"
    if (Test-Path -LiteralPath $PointerFile) {
        Assert-NotReparsePoint $PointerFile "Current Agent pointer"
        $CurrentPointer = Get-Content -LiteralPath $PointerFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if ($null -eq $ExpectedPointer) {
        if ($null -ne $CurrentPointer) { return $false }
    }
    else {
        $Fields = @("activation_id", "command_id", "relative_executable", "schema_version", "version")
        if ($null -eq $CurrentPointer -or
            (Compare-Object @($CurrentPointer.PSObject.Properties.Name | Sort-Object) $Fields).Count -ne 0) { return $false }
        foreach ($Field in $Fields) {
            if ([string]$CurrentPointer.$Field -cne [string]$ExpectedPointer.$Field) { return $false }
        }
    }
    if (-not [string]::Equals([IO.Path]::GetFullPath((Join-Path $PortableRoot ($Relative.Replace('/', '\')))),
        [IO.Path]::GetFullPath($AgentExe), [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ($ExpectedHash -notmatch '^[0-9a-f]{64}$' -or
        (Get-FileHash -LiteralPath $AgentExe -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ExpectedHash) { return $false }
    $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$State.agent_id -cne [string]$Intent.agent_id) { return $false }
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { $Hash = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$State.agent_secret)) }
    finally { $Hasher.Dispose() }
    return (-join ($Hash | ForEach-Object { $_.ToString("x2") })) -ceq [string]$Intent.identity_sha256
}

function Stop-PortableAgent {
    $Targets = @()
    if (Test-Path -LiteralPath $PidPath -PathType Leaf) {
        try {
            $Record = Get-Content -LiteralPath $PidPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $Process = Get-Process -Id ([int]$Record.pid) -ErrorAction Stop
            $ProcessInfo = @(Get-WindowsProcessInfo `
                -Filter "ProcessId=$($Process.Id)" `
                -FallbackName "RigPilotAgent" `
                -FallbackId $Process.Id) | Select-Object -First 1
            if ($null -eq $ProcessInfo) {
                throw "The PID record does not refer to a running process."
            }
            $ExpectedPath = [IO.Path]::GetFullPath([string]$Record.executable_path)
            $ActualPath = [IO.Path]::GetFullPath([string]$ProcessInfo.ExecutablePath)
            $ExpectedStart = [DateTime]::Parse([string]$Record.started_at).ToUniversalTime()
            $ActualStart = $Process.StartTime.ToUniversalTime()
            $StartDelta = [Math]::Abs(($ActualStart - $ExpectedStart).TotalSeconds)
            if ([string]::Equals($ExpectedPath, $ActualPath, [StringComparison]::OrdinalIgnoreCase) -and
                [string]::Equals($ActualPath, [IO.Path]::GetFullPath($AgentExe), [StringComparison]::OrdinalIgnoreCase) -and
                $StartDelta -lt 1) {
                $Targets += $ProcessInfo
            }
        }
        catch {
            Write-Host "Ignoring a stale or invalid PID record."
        }
    }
    if ($Targets.Count -eq 0) {
        $Targets = @(Get-OwnedAgentProcesses)
    }
    if ($Targets.Count -eq 0) {
        if (Test-Path -LiteralPath $PidPath) {
            Remove-Item -LiteralPath $PidPath -Force
        }
        Write-Host "RigPilot portable Agent is not running; nothing needs to be stopped." -ForegroundColor Yellow
        return
    }
    foreach ($Target in $Targets) {
        Stop-Process -Id ([int]$Target.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $PidPath) {
        Remove-Item -LiteralPath $PidPath -Force
    }
    if (@(Get-OwnedAgentProcesses).Count -gt 0) {
        throw "The portable Agent is still running."
    }
    Write-Host "RigPilot portable Agent is stopped." -ForegroundColor Green
}

function Invoke-PortableForegroundAgentRun([bool]$ScheduledMode) {
    # Shared foreground run for the Start action and the Install-Autostart
    # failure fallback: launch this folder's Agent in the current console
    # under the launch mutex, publish its PID record, wait for exit, clean
    # the record, and return the Agent exit code.  Enrollment is refreshed
    # idempotently (a saved identity returns immediately).
    $LaunchMutex = Enter-PortableLaunchMutex
    try {
        $RecoveryContext = [Environment]::GetEnvironmentVariable("RIGPILOT_UPDATE_RECOVERY_ID", "Process")
        [Environment]::SetEnvironmentVariable("RIGPILOT_UPDATE_RECOVERY_ID", $null, "Process")
        if (-not [string]::IsNullOrEmpty($RecoveryContext)) { $script:AgentExe = Resolve-PortableAgentExe }
        if (-not [string]::IsNullOrEmpty($RecoveryContext) -and
            (-not $ScheduledMode -or -not (Test-PortableRecoveryLaunchAllowed $RecoveryContext))) {
            return 0
        }
        if (@(Get-OwnedAgentProcesses).Count -gt 0) {
            Write-Host "RigPilot portable Agent is already running." -ForegroundColor Yellow
            return 0
        }
        Initialize-PortableEnrollment $ScheduledMode

        # Use a single command-line string for the same Windows PowerShell
        # 5.1 quoting reason as Restart-Elevated above.
        $Arguments = '--config "' + $ConfigPath + '"'
        $AgentProcess = Start-Process `
            -FilePath $AgentExe `
            -ArgumentList $Arguments `
            -WorkingDirectory (Split-Path -Parent $AgentExe) `
            -NoNewWindow `
            -PassThru
        $Record = [ordered]@{
            pid = $AgentProcess.Id
            executable_path = [IO.Path]::GetFullPath($AgentExe)
            started_at = $AgentProcess.StartTime.ToUniversalTime().ToString("o")
        }
        $TemporaryPid = Join-Path $DataDir (".agent.pid." + [Guid]::NewGuid().ToString("N") + ".tmp")
        [IO.File]::WriteAllText(
            $TemporaryPid,
            (($Record | ConvertTo-Json -Compress) + [Environment]::NewLine),
            $Utf8NoBom
        )
        if (Test-Path -LiteralPath $PidPath) {
            Remove-Item -LiteralPath $PidPath -Force
        }
        [IO.File]::Move($TemporaryPid, $PidPath)
    }
    finally {
        Exit-PortableLaunchMutex $LaunchMutex
    }

    if (-not $ScheduledMode) {
        Write-Host "RigPilot portable Agent is running. Keep this window open." -ForegroundColor Green
    }
    try {
        $AgentProcess.WaitForExit()
        $AgentExitCode = $AgentProcess.ExitCode
    }
    finally {
        if (Test-Path -LiteralPath $PidPath) {
            try {
                $CurrentRecord = Get-Content -LiteralPath $PidPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ([int]$CurrentRecord.pid -eq $AgentProcess.Id) {
                    Remove-Item -LiteralPath $PidPath -Force
                }
            }
            catch {
                Write-Host "Could not clean the PID record: $($_.Exception.Message)" -ForegroundColor Yellow
            }
        }
    }
    return $AgentExitCode
}

try {
    if ($Scheduled -and $Action -ne "Start") {
        throw "-Scheduled is valid only with -Action Start."
    }
    if ($BootstrapEnrollment -and $Action -ne "InstallAutostart") {
        throw "-BootstrapEnrollment is valid only with -Action InstallAutostart."
    }
    if ($MigrationInstall -and ($Action -ne "InstallAutostart" -or $BootstrapEnrollment)) {
        throw "-MigrationInstall requires -Action InstallAutostart without enrollment bootstrap."
    }
    if ($Action -in @("Start", "InstallAutostart") -and
        -not (Test-Path -LiteralPath $AgentExe -PathType Leaf)) {
        throw "RigPilotAgent.exe is missing. Extract the complete portable ZIP before running."
    }
    Assert-NotReparsePoint $PortableRoot "Portable directory"

    if ($Action -eq "CheckConnection") {
        if (Test-PortableControllerConnection) {
            exit 0
        }
        exit 1
    }

    # SRBMiner/NVML clock and power controls require an elevated Windows
    # process.  Elevate the portable launcher itself so the Agent and every
    # managed miner inherit the required token without installing a service.
    if (-not (Test-IsAdministrator)) {
        if ($MigrationInstall) {
            throw "The migration coordinator must already be elevated."
        }
        $EnrollmentCodePresent = -not [string]::IsNullOrEmpty(
            [Environment]::GetEnvironmentVariable(
                "RIGPILOT_ENROLLMENT_TOKEN", [EnvironmentVariableTarget]::Process
            )
        )
        $ReplacementCodePresent = -not [string]::IsNullOrEmpty(
            [Environment]::GetEnvironmentVariable(
                "RIGPILOT_REPLACEMENT_TOKEN", [EnvironmentVariableTarget]::Process
            )
        )
        if ($Action -in @("Start", "InstallAutostart") -and
            ($EnrollmentCodePresent -or $ReplacementCodePresent)) {
            throw "Explicit first-contact codes require an already elevated PowerShell session; UAC relaunch cannot safely carry process environment."
        }
        $ElevatedExitCode = Restart-Elevated `
            -RequestedAction $Action `
            -RequestedScheduled ([bool]$Scheduled) `
            -RequestedBootstrapEnrollment ([bool]$BootstrapEnrollment)
        exit $ElevatedExitCode
    }

    if ($Action -eq "InstallAutostart") {
        # Start-RigPilotAgent.bat is allowed to change during a remote update,
        # while this trusted launcher is deliberately retained.  Therefore the
        # batch file invokes only the legacy InstallAutostart action and this
        # launcher decides, after elevation, whether enrollment is required.
        # A complete existing identity never re-enters the interactive flow.
        $EnrollmentRequired = -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $StatePath -PathType Leaf)
        if ($MigrationInstall -and $EnrollmentRequired) {
            throw "The migration target has no complete enrollment state."
        }
        if ($BootstrapEnrollment -or $EnrollmentRequired) {
            $LaunchMutex = Enter-PortableLaunchMutex
            try {
                if (@(Get-OwnedAgentProcesses).Count -eq 0) {
                    Initialize-PortableEnrollment $false
                }
                elseif (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or
                    -not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
                    throw "A running Agent has no complete local enrollment state. Stop it and retry."
                }
            }
            finally {
                Exit-PortableLaunchMutex $LaunchMutex
                $LaunchMutex = $null
            }
        }
        else {
            # An existing identity never uses a supplied first-contact code.
            # Do not pass it to the migration or scheduled-task child process.
            [Environment]::SetEnvironmentVariable(
                "RIGPILOT_ENROLLMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
            )
            [Environment]::SetEnvironmentVariable(
                "RIGPILOT_REPLACEMENT_TOKEN", $null, [EnvironmentVariableTarget]::Process
            )
        }
        try {
            Install-PortableAutostart
        }
        catch {
            # Never leave a fully enrolled device registered-but-offline just
            # because migration or autostart installation failed (the mode the
            # controller cannot observe or dispatch to at all).  Fall back to
            # the supported foreground run from this folder; no SYSTEM task is
            # ever pointed at a non-protected folder, and a later
            # Install-Autostart retry migrates this same identity.
            if ($MigrationInstall -or
                -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf) -or
                -not (Test-PortableEnrolledIdentityFile) -or
                @(Get-OwnedAgentProcesses).Count -gt 0 -or
                (Test-PortableProtectedAgentRunning)) {
                throw
            }
            $AutostartFailure = $_.Exception.Message
            Write-PortableLauncherFailureLog $AutostartFailure
            Write-Host ("ERROR: " + $AutostartFailure) -ForegroundColor Red
            Write-Host "" 
            Write-Host "Autostart installation failed; keeping the enrolled device online instead." -ForegroundColor Yellow
            Write-Host "The Agent will now run in the foreground from this folder. KEEP THIS WINDOW OPEN." -ForegroundColor Yellow
            Write-Host "Fix the reported error, then run Install-Autostart.bat again; the retry migrates this same identity." -ForegroundColor Yellow
            Invoke-PortableForegroundAgentRun $false | Out-Null
            # Foreground availability is not a successful autostart install.
            # Keep the original setup failure visible to the calling BAT.
            exit 1
        }
        exit 0
    }

    if ($Action -eq "RemoveAutostart") {
        $StopMutex = Enter-PortableLaunchMutex
        try { Cancel-PortableUpdateRecoveryIntent; Remove-PortableAutostart }
        finally { Exit-PortableLaunchMutex $StopMutex }
        exit 0
    }

    if ($Action -eq "Stop") {
        $StopMutex = Enter-PortableLaunchMutex
        try { Cancel-PortableUpdateRecoveryIntent; Stop-PortableAgent }
        finally { Exit-PortableLaunchMutex $StopMutex }
        exit 0
    }

    exit (Invoke-PortableForegroundAgentRun ([bool]$Scheduled))
}
catch {
    $FailureMessage = $_.Exception.Message
    Write-PortableLauncherFailureLog $FailureMessage
    if ($null -ne $LaunchMutex) {
        try {
            Exit-PortableLaunchMutex $LaunchMutex
        }
        catch {
            # The original failure is more useful than a cleanup failure.
        }
    }
    Write-Host "ERROR: $FailureMessage" -ForegroundColor Red
    exit 1
}
