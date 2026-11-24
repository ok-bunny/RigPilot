[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceRoot
)

# This coordinator is intentionally separate from portable-agent.ps1.  It runs
# only after UAC elevation, copies an enrolled legacy portable folder into the
# fixed protected root, then re-executes the launcher from that protected root.
# It never registers or starts a SYSTEM task whose action points at SourceRoot.
$ErrorActionPreference = "Stop"
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
$ProgramDataRoot = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::CommonApplicationData
)
if ([string]::IsNullOrWhiteSpace($ProgramDataRoot) -or
    -not [IO.Path]::IsPathRooted($ProgramDataRoot)) {
    throw "Windows CommonApplicationData is unavailable."
}
$ProgramDataRoot = [IO.Path]::GetFullPath($ProgramDataRoot).TrimEnd('\')
$TargetRoot = [IO.Path]::GetFullPath(
    (Join-Path $ProgramDataRoot "RigPilotAgentPortable")
).TrimEnd('\')
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$StagingRoot = $null
$BackupRoot = $null
$TargetExisted = $false
$TargetTaskExisted = $false
$TargetRecoveryTaskExisted = $false
$SourceWasRunning = $false
$TargetWasRunning = $false
$SourceMinerWasRunning = $false
$TargetMinerWasRunning = $false
$TargetMinerStarted = $false
$SourceTaskRemoved = $false
$OverlayRecords = @()
$NewDirectories = @()
$MigrationMutex = $null
$MigrationMutexAcquired = $false
$MutationStarted = $false
$SourceAgentStopAttempted = $false
$TargetAgentStopAttempted = $false
$MinerStopAttempted = $false
$TargetCreated = $false
$TargetOverlayStarted = $false
$TargetTaskInstallAttempted = $false
$OwnedTemporaryRoots = @()

function Test-IsAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Assert-PlainPath([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Label does not exist: $Path"
    }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a symbolic link, junction, or mount point: $Path"
    }
}

function Assert-PlainPathComponents([string]$Path, [string]$Label) {
    $Current = Get-Item -LiteralPath $Path -Force
    while ($null -ne $Current) {
        if (($Current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a symbolic link, junction, or mount point: $($Current.FullName)"
        }
        $Parent = [IO.Directory]::GetParent($Current.FullName)
        if ($null -eq $Parent) { break }
        $Current = Get-Item -LiteralPath $Parent.FullName -Force
    }
}

function Get-PlainTreeEntries([string]$Root, [bool]$ExcludeData) {
    Assert-PlainPath $Root "Portable tree"
    $RootPrefix = $Root.TrimEnd('\') + '\'
    $Stack = New-Object System.Collections.Stack
    $Stack.Push($Root)
    $Count = 0
    while ($Stack.Count -gt 0) {
        $Directory = [string]$Stack.Pop()
        foreach ($Entry in @([IO.Directory]::EnumerateFileSystemEntries($Directory))) {
            $Count += 1
            if ($Count -gt 200000) {
                throw "Portable tree contains too many entries."
            }
            $Attributes = [IO.File]::GetAttributes($Entry)
            if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Portable tree contains a link or junction: $Entry"
            }
            $Full = [IO.Path]::GetFullPath($Entry)
            if (-not $Full.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Portable tree entry escapes its root: $Entry"
            }
            $Relative = $Full.Substring($RootPrefix.Length)
            $First = $Relative.Split('\')[0]
            if ($ExcludeData -and [string]::Equals(
                $First, "data", [StringComparison]::OrdinalIgnoreCase
            )) {
                continue
            }
            $IsDirectory = (($Attributes -band [IO.FileAttributes]::Directory) -ne 0)
            [PSCustomObject]@{
                Source = $Full
                Relative = $Relative
                IsDirectory = $IsDirectory
            }
            if ($IsDirectory) { $Stack.Push($Full) }
        }
    }
}

function Assert-PlainTree([string]$Root) {
    @(Get-PlainTreeEntries $Root $false) | Out-Null
}

function Join-CheckedTreePath([string]$Root, [string]$Relative) {
    $Prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $Candidate = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $Candidate.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Migration destination escapes its root."
    }
    return $Candidate
}

function New-RestrictedDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -ErrorAction Stop | Out-Null
    # Record ownership immediately after Create succeeds, including ACL errors.
    $script:OwnedTemporaryRoots += $Path
    $SystemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $AdministratorsSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $Inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $Acl = New-Object Security.AccessControl.DirectorySecurity
    $Acl.SetAccessRuleProtection($true, $false)
    $Acl.SetOwner($AdministratorsSid)
    foreach ($Sid in @($SystemSid, $AdministratorsSid)) {
        $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $Sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $Inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )))
    }
    Set-Acl -LiteralPath $Path -AclObject $Acl
}

function Copy-PlainTree([string]$From, [string]$To, [bool]$ExcludeData) {
    foreach ($Entry in @(Get-PlainTreeEntries $From $ExcludeData)) {
        $Destination = Join-CheckedTreePath $To $Entry.Relative
        if ($Entry.IsDirectory) {
            [IO.Directory]::CreateDirectory($Destination) | Out-Null
            continue
        }
        [IO.Directory]::CreateDirectory((Split-Path -Parent $Destination)) | Out-Null
        [IO.File]::Copy($Entry.Source, $Destination, $false)
    }
}

function Copy-FileAtomically([string]$From, [string]$To) {
    $Temporary = $To + ".migration." + [Guid]::NewGuid().ToString("N") + ".tmp"
    $NativeBackup = $To + ".migration." + [Guid]::NewGuid().ToString("N") + ".bak"
    try {
        [IO.Directory]::CreateDirectory((Split-Path -Parent $To)) | Out-Null
        [IO.File]::Copy($From, $Temporary, $false)
        if (Test-Path -LiteralPath $To -PathType Leaf) {
            [IO.File]::Replace($Temporary, $To, $NativeBackup, $true)
        }
        else {
            [IO.File]::Move($Temporary, $To)
        }
    }
    finally {
        foreach ($TemporaryPath in @($Temporary, $NativeBackup)) {
            if (Test-Path -LiteralPath $TemporaryPath -PathType Leaf) {
                Remove-Item -LiteralPath $TemporaryPath -Force
            }
        }
    }
}

function Read-EnrollmentIdentity([string]$Root) {
    $Data = Join-Path $Root "data"
    $Config = Join-Path $Data "config.json"
    $State = Join-Path $Data "state.json"
    foreach ($Pair in @(
        @($Data, "Portable data directory"),
        @($Config, "Portable config"),
        @($State, "Portable device state")
    )) {
        Assert-PlainPath $Pair[0] $Pair[1]
    }
    foreach ($File in @($Config, $State)) {
        $Item = Get-Item -LiteralPath $File -Force
        if ($Item.PSIsContainer -or $Item.Length -gt 4MB) {
            throw "Portable identity/config file is unsafe: $File"
        }
    }
    $Document = Get-Content -LiteralPath $State -Raw -Encoding UTF8 | ConvertFrom-Json
    $AgentId = [string]$Document.agent_id
    $AgentSecret = [string]$Document.agent_secret
    if ($AgentId -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$' -or
        [string]::IsNullOrWhiteSpace($AgentSecret) -or
        $AgentSecret.Length -gt 2048) {
        throw "Portable device state has no valid enrollment identity."
    }
    return [PSCustomObject]@{ AgentId = $AgentId; AgentSecret = $AgentSecret }
}

function Assert-PortableBundleManifest([string]$Root) {
    $ManifestPath = Join-Path $Root "agent-release.json"
    Assert-PlainPath $ManifestPath "Portable release manifest"
    $ManifestItem = Get-Item -LiteralPath $ManifestPath -Force
    if ($ManifestItem.PSIsContainer -or $ManifestItem.Length -gt 8MB) {
        throw "Portable release manifest is unsafe."
    }
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Names = @($Manifest.PSObject.Properties.Name | Sort-Object)
    $ExpectedNames = @("architecture", "files", "platform", "schema_version", "version")
    if ((Compare-Object $Names $ExpectedNames).Count -ne 0 -or
        $Manifest.schema_version -is [bool] -or
        [int]$Manifest.schema_version -ne 1 -or
        [string]$Manifest.platform -ne "windows" -or
        [string]$Manifest.architecture -ne "x86_64" -or
        [string]$Manifest.version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
        $null -eq $Manifest.files) {
        throw "Portable release manifest is invalid."
    }
    $Required = @{
        "RigPilotAgent.exe" = $false
        "portable-agent.ps1" = $false
        "portable-migrate.ps1" = $false
        "RigPilotAgentUpdater.ps1" = $false
    }
    $RootPrefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $Count = 0
    foreach ($Property in @($Manifest.files.PSObject.Properties)) {
        $Count += 1
        if ($Count -gt 200000) {
            throw "Portable release manifest contains too many files."
        }
        $Relative = [string]$Property.Name
        $Digest = [string]$Property.Value
        $First = $Relative.Split('/')[0]
        if ([string]::IsNullOrWhiteSpace($Relative) -or
            [string]::Equals($First, "data", [StringComparison]::OrdinalIgnoreCase) -or
            [string]::Equals($First, "versions", [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::IsPathRooted($Relative) -or
            $Relative.Contains('\') -or
            $Relative.Contains(':') -or
            $Relative.StartsWith('/') -or
            $Relative.Split('/') -contains ".." -or
            $Relative.Split('/') -contains "." -or
            $Digest -notmatch '^[0-9a-fA-F]{64}$') {
            throw "Portable release manifest contains an unsafe file entry."
        }
        $Candidate = [IO.Path]::GetFullPath(
            (Join-Path $Root $Relative.Replace('/', '\'))
        )
        if (-not $Candidate.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Portable release manifest escapes its root."
        }
        Assert-PlainPath $Candidate "Portable release file"
        if ((Get-Item -LiteralPath $Candidate -Force).PSIsContainer) {
            throw "Portable release manifest points to a directory."
        }
        $Actual = (Get-FileHash -LiteralPath $Candidate -Algorithm SHA256).Hash
        if (-not [string]::Equals($Actual, $Digest, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Portable release file hash does not match: $Relative"
        }
        if ($Required.ContainsKey($Relative)) { $Required[$Relative] = $true }
    }
    if (@($Required.Values | Where-Object { -not $_ }).Count -gt 0) {
        throw "Portable release manifest is missing a required migration component."
    }
}

function Get-PortableManifestProgramFiles([string]$Root) {
    Assert-PortableBundleManifest $Root
    $ManifestPath = Join-CheckedTreePath $Root "agent-release.json"
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Files = @{}
    $Files["agent-release.json"] = "agent-release.json"
    foreach ($Property in @($Manifest.files.PSObject.Properties)) {
        $Relative = ([string]$Property.Name).Replace('/', '\')
        $First = $Relative.Split('\')[0]
        if ([string]::Equals($First, "data", [StringComparison]::OrdinalIgnoreCase) -or
            [string]::Equals($First, "versions", [StringComparison]::OrdinalIgnoreCase)) {
            throw "Portable program manifest cannot include data or versions: $Relative"
        }
        if ($Files.ContainsKey($Relative)) {
            throw "Portable program manifest contains a duplicate path: $Relative"
        }
        $Files[$Relative] = $Relative
    }
    return $Files
}

function Write-JsonAtomically([string]$Path, $Value) {
    $Temporary = $Path + ".migration." + [Guid]::NewGuid().ToString("N") + ".tmp"
    $Backup = $Path + ".migration." + [Guid]::NewGuid().ToString("N") + ".bak"
    $Encoding = New-Object Text.UTF8Encoding($false)
    try {
        [IO.File]::WriteAllText(
            $Temporary,
            (($Value | ConvertTo-Json -Depth 16) + [Environment]::NewLine),
            $Encoding
        )
        [IO.File]::Replace($Temporary, $Path, $Backup, $true)
    }
    finally {
        foreach ($TemporaryPath in @($Temporary, $Backup)) {
            if (Test-Path -LiteralPath $TemporaryPath -PathType Leaf) {
                Remove-Item -LiteralPath $TemporaryPath -Force
            }
        }
    }
}

function Test-PathInside([string]$Path, [string]$Root) {
    $Full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $Boundary = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    return (
        [string]::Equals($Full, $Boundary, [StringComparison]::OrdinalIgnoreCase) -or
        $Full.StartsWith($Boundary + '\', [StringComparison]::OrdinalIgnoreCase)
    )
}

function Resolve-SourceDataPath([string]$Value, [string]$SourceData) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "Portable config contains an empty data path."
    }
    $Resolved = if ([IO.Path]::IsPathRooted($Value)) {
        [IO.Path]::GetFullPath($Value)
    } else {
        [IO.Path]::GetFullPath((Join-Path $SourceData $Value))
    }
    if (-not (Test-PathInside $Resolved $SourceData)) {
        throw "Portable config data paths must stay inside the legacy data directory."
    }
    return $Resolved
}

function Update-MigratedDataPaths([string]$FromRoot, [string]$ToRoot) {
    $SourceData = [IO.Path]::GetFullPath((Join-Path $FromRoot "data")).TrimEnd('\')
    $TargetData = [IO.Path]::GetFullPath((Join-Path $ToRoot "data")).TrimEnd('\')
    $ConfigPath = Join-Path $TargetData "config.json"
    Assert-PlainPath $ConfigPath "Migrated Agent config"
    $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $DataPathNames = @(
        "state_file", "log_file", "flight_sheet_file", "miner_status_file",
        "managed_miner_runtime_dir", "miner_packages_dir"
    )
    $ResolvedSource = @{}
    foreach ($Name in $DataPathNames) {
        $Property = $Config.PSObject.Properties[$Name]
        if ($null -eq $Property -or $null -eq $Property.Value) { continue }
        if (-not ($Property.Value -is [string])) {
            throw "Portable config contains a non-string data path: $Name"
        }
        $Raw = [string]$Property.Value
        $Original = if ([IO.Path]::IsPathRooted($Raw) -and (Test-PathInside $Raw $TargetData)) {
            [IO.Path]::GetFullPath($Raw)
        } else {
            Resolve-SourceDataPath $Raw $SourceData
        }
        $Relative = if (Test-PathInside $Original $TargetData) {
            $Original.Substring($TargetData.Length).TrimStart('\')
        } else {
            $Original.Substring($SourceData.Length).TrimStart('\')
        }
        if ([string]::IsNullOrWhiteSpace($Relative)) {
            throw "Portable config data path cannot point at the entire data directory: $Name"
        }
        $ResolvedSource[$Name] = if (Test-PathInside $Original $TargetData) {
            Join-Path $SourceData $Relative
        } else {
            $Original
        }
        $Config | Add-Member -NotePropertyName $Name -NotePropertyValue $Relative -Force
    }
    Write-JsonAtomically $ConfigPath $Config

    if (-not $ResolvedSource.ContainsKey("flight_sheet_file") -or
        -not $ResolvedSource.ContainsKey("miner_packages_dir")) {
        return
    }
    $FlightRelative = [string]$Config.flight_sheet_file
    $PackagesRelative = [string]$Config.miner_packages_dir
    $FlightPath = [IO.Path]::GetFullPath((Join-Path $TargetData $FlightRelative))
    $TargetPackages = [IO.Path]::GetFullPath((Join-Path $TargetData $PackagesRelative))
    if (-not (Test-Path -LiteralPath $FlightPath -PathType Leaf)) { return }
    Assert-PlainPath $FlightPath "Migrated active flight sheet"
    Assert-PlainPath $TargetPackages "Migrated miner packages directory"
    $SourcePackages = [IO.Path]::GetFullPath([string]$ResolvedSource["miner_packages_dir"])
    $Flight = Get-Content -LiteralPath $FlightPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($LegName in @("primary", "secondary")) {
        $Leg = $Flight.PSObject.Properties[$LegName]
        if ($null -eq $Leg -or $null -eq $Leg.Value) { continue }
        $Package = $Leg.Value.PSObject.Properties["miner_package"]
        if ($null -eq $Package -or $null -eq $Package.Value) { continue }
        $Installed = $Package.Value.PSObject.Properties["installed_path"]
        if ($null -eq $Installed -or [string]::IsNullOrWhiteSpace([string]$Installed.Value)) {
            continue
        }
        $OldInstalled = [IO.Path]::GetFullPath([string]$Installed.Value)
        if (Test-PathInside $OldInstalled $TargetPackages) {
            $NewInstalled = $OldInstalled
        }
        elseif (Test-PathInside $OldInstalled $SourcePackages) {
            $PackageRelative = $OldInstalled.Substring($SourcePackages.TrimEnd('\').Length).TrimStart('\')
            if ([string]::IsNullOrWhiteSpace($PackageRelative)) {
                throw "Flight-sheet miner package cannot point at the package root itself."
            }
            $NewInstalled = [IO.Path]::GetFullPath((Join-Path $TargetPackages $PackageRelative))
        }
        else {
            throw "Flight-sheet miner package path is outside the legacy package directory."
        }
        if (-not (Test-Path -LiteralPath $NewInstalled -PathType Container)) {
            throw "Migrated flight-sheet miner package is missing: $PackageRelative"
        }
        Assert-PlainPath $NewInstalled "Migrated flight-sheet miner package"
        $Package.Value | Add-Member `
            -NotePropertyName "installed_path" `
            -NotePropertyValue $NewInstalled `
            -Force
    }
    Write-JsonAtomically $FlightPath $Flight
}

function Remove-UnmanifestedProgramFiles([string]$Root) {
    $ManifestPath = Join-Path $Root "agent-release.json"
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Allowed = @{}
    $Allowed["agent-release.json"] = $true
    foreach ($Property in @($Manifest.files.PSObject.Properties)) {
        $Allowed[[string]$Property.Name] = $true
    }
    $Entries = @(Get-PlainTreeEntries $Root $true)
    foreach ($Entry in @($Entries | Where-Object { -not $_.IsDirectory })) {
        $Canonical = $Entry.Relative.Replace('\', '/')
        if (-not $Allowed.ContainsKey($Canonical)) {
            Remove-Item -LiteralPath $Entry.Source -Force
        }
    }
    foreach ($Entry in @($Entries | Where-Object { $_.IsDirectory } | Sort-Object { $_.Relative.Length } -Descending)) {
        if (@([IO.Directory]::EnumerateFileSystemEntries($Entry.Source)).Count -eq 0) {
            [IO.Directory]::Delete($Entry.Source, $false)
        }
    }
}

function Get-PortableTaskIdentity([string]$Root) {
    $Normalized = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/').ToLowerInvariant()
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { $Hash = $Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Normalized)) }
    finally { $Hasher.Dispose() }
    return -join ($Hash[0..5] | ForEach-Object { $_.ToString("x2") })
}

function Test-SystemTaskPrincipal($Principal) {
    if ($null -eq $Principal) { return $false }
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

function Test-OwnedTaskForRoot($Task, [string]$Root, [bool]$Recovery = $false) {
    if ($null -eq $Task) { return $false }
    $ScriptPath = [IO.Path]::GetFullPath((Join-Path $Root "portable-agent.ps1"))
    $ExpectedArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $ScriptPath + '" -Action Start -Scheduled'
    if ($Recovery) {
        $UpdaterPath = [IO.Path]::GetFullPath((Join-Path $Root "RigPilotAgentUpdater.ps1"))
        $ExpectedArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
            ' -File "' + $UpdaterPath + '" -Action Recover -PortableRoot "' + $Root + '"'
    }
    $Actions = @($Task.Actions)
    if ($Actions.Count -ne 1) { return $false }
    return (
        [string]::Equals([string]$Actions[0].Execute, $PowerShellPath,
            [StringComparison]::OrdinalIgnoreCase) -and
        [string]::Equals([string]$Actions[0].WorkingDirectory, $Root,
            [StringComparison]::OrdinalIgnoreCase) -and
        [string]$Actions[0].Arguments -ceq $ExpectedArguments -and
        (Test-SystemTaskPrincipal $Task.Principal) -and
        [string]$Task.Principal.RunLevel -eq "Highest" -and
        ([string]::IsNullOrEmpty([string]$Task.TaskPath) -or [string]$Task.TaskPath -eq "\")
    )
}

function Get-CheckedTaskForRoot([string]$Root, [bool]$Recovery = $false) {
    $Prefix = if ($Recovery) { "RigPilotAgentUpdateRecovery" } else { "RigPilotAgentPortable" }
    $Name = "$Prefix-$(Get-PortableTaskIdentity $Root)"
    $Task = Get-ScheduledTask -TaskName $Name -TaskPath "\" -ErrorAction SilentlyContinue
    if ($null -ne $Task -and -not (Test-OwnedTaskForRoot $Task $Root $Recovery)) {
        throw "Scheduled task collision; refusing to replace or remove: $Name"
    }
    return $Task
}

function Test-AgentExecutableForRoot([string]$Path, [string]$Root) {
    try { $Actual = [IO.Path]::GetFullPath($Path) } catch { return $false }
    $Base = [IO.Path]::GetFullPath((Join-Path $Root "RigPilotAgent.exe"))
    if ([string]::Equals($Actual, $Base, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $Versions = [IO.Path]::GetFullPath((Join-Path $Root "versions")).TrimEnd('\') + '\'
    if (-not $Actual.StartsWith($Versions, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    $Parts = $Actual.Substring($Versions.Length).Split('\')
    return ($Parts.Count -eq 2 -and
        $Parts[0] -match '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -and
        [string]::Equals($Parts[1], "RigPilotAgent.exe", [StringComparison]::OrdinalIgnoreCase))
}

function Get-AgentProcessesForRoot([string]$Root) {
    foreach ($Process in @(Get-Process -Name "RigPilotAgent" -ErrorAction SilentlyContinue)) {
        $Path = $null
        try { $Path = [string]$Process.Path } catch {}
        if ([string]::IsNullOrWhiteSpace($Path)) {
            try { $Path = [string]$Process.MainModule.FileName } catch { continue }
        }
        if (Test-AgentExecutableForRoot $Path $Root) { $Process }
    }
}

function Stop-AgentForRoot([string]$Root) {
    $Processes = @(Get-AgentProcessesForRoot $Root)
    foreach ($Process in $Processes) {
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        Wait-Process -Id $Process.Id -Timeout 15 -ErrorAction SilentlyContinue
    }
    if (@(Get-AgentProcessesForRoot $Root).Count -gt 0) {
        throw "The Agent owned by this portable folder could not be stopped: $Root"
    }
    $AgentPidPath = Join-Path (Join-Path $Root "data") "agent.pid.json"
    if (Test-Path -LiteralPath $AgentPidPath) {
        Assert-PlainPath $AgentPidPath "Portable Agent PID record"
        Remove-Item -LiteralPath $AgentPidPath -Force
    }
    return $Processes.Count
}

function Test-LiveManagedMiner([string]$Root) {
    $ConfigPath = Join-Path (Join-Path $Root "data") "config.json"
    $Config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $RuntimeName = [string]$Config.managed_miner_runtime_dir
    if ([string]::IsNullOrWhiteSpace($RuntimeName)) { return $false }
    $Runtime = if ([IO.Path]::IsPathRooted($RuntimeName)) {
        [IO.Path]::GetFullPath($RuntimeName)
    } else {
        [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $ConfigPath) $RuntimeName))
    }
    $DataPrefix = [IO.Path]::GetFullPath((Join-Path $Root "data")).TrimEnd('\') + '\'
    if (-not ($Runtime.TrimEnd('\') + '\').StartsWith(
        $DataPrefix, [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Managed miner runtime must stay inside portable data before migration."
    }
    $RecordPath = Join-Path $Runtime "managed-miner-process.json"
    if (-not (Test-Path -LiteralPath $RecordPath)) { return $false }
    Assert-PlainPath $RecordPath "Managed miner process record"
    $Item = Get-Item -LiteralPath $RecordPath -Force
    if ($Item.PSIsContainer -or $Item.Length -gt 32768) {
        throw "Managed miner process record is unsafe."
    }
    $Record = Get-Content -LiteralPath $RecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $MinerPid = $Record.pid
    if ($MinerPid -is [bool] -or $null -eq $MinerPid -or [int]$MinerPid -le 0) {
        throw "Managed miner process record is invalid."
    }
    $Miner = Get-Process -Id ([int]$MinerPid) -ErrorAction SilentlyContinue
    if ($null -eq $Miner) { return $false }
    $Actual = $null
    try { $Actual = [IO.Path]::GetFullPath([string]$Miner.Path) } catch {}
    if ([string]::IsNullOrWhiteSpace($Actual)) {
        try { $Actual = [IO.Path]::GetFullPath([string]$Miner.MainModule.FileName) } catch {}
    }
    $Recorded = [IO.Path]::GetFullPath([string]$Record.executable)
    $RecordedStart = [double]$Record.create_time
    $ActualStart = ([DateTimeOffset]$Miner.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds() / 1000.0
    if (-not [string]::Equals($Actual, $Recorded, [StringComparison]::OrdinalIgnoreCase) -or
        [Math]::Abs($ActualStart - $RecordedStart) -gt 0.5) {
        # A reboot commonly leaves a process record whose PID has since been
        # reused.  It is stale, not a live RigPilot-owned miner.  Never stop the
        # unrelated process, but do not let the stale record block a same-
        # identity program overlay either.
        Write-Host "Ignoring a stale managed miner PID record during migration." `
            -ForegroundColor Yellow
        return $false
    }
    return $true
}

function Invoke-ManagedMinerOnce(
    [string]$RunnerRoot,
    [string]$ConfigRoot,
    [ValidateSet("Stop", "Resume")][string]$Operation
) {
    $Runner = Join-Path $RunnerRoot "RigPilotAgent.exe"
    $Config = Join-Path (Join-Path $ConfigRoot "data") "config.json"
    foreach ($Pair in @(
        @($Runner, "Migration Agent executable"),
        @($Config, "Migration Agent config")
    )) {
        Assert-PlainPath $Pair[0] $Pair[1]
    }
    $Flag = if ($Operation -eq "Stop") {
        "--stop-managed-mining-once"
    } else {
        "--resume-managed-mining-once"
    }
    & $Runner --config $Config $Flag | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Could not $($Operation.ToLowerInvariant()) the managed miner safely (exit $LASTEXITCODE)."
    }
}

function Apply-ProgramOverlay([string]$From, [string]$To, [string]$Backup) {
    # Treat the signed-by-hash release manifest as the complete program input.
    # Stale files left beside an overlaid legacy package (including version
    # slots) are never copied into the protected installation.
    $NewFiles = Get-PortableManifestProgramFiles $From
    $OldFiles = Get-PortableManifestProgramFiles $To
    $DirectoryRelatives = @{}
    foreach ($Relative in @($NewFiles.Values)) {
        $Parent = Split-Path -Parent $Relative
        while (-not [string]::IsNullOrWhiteSpace($Parent)) {
            $DirectoryRelatives[$Parent] = $Parent
            $Parent = Split-Path -Parent $Parent
        }
    }
    $Entries = @(
        foreach ($Relative in @($DirectoryRelatives.Values | Sort-Object Length)) {
            [PSCustomObject]@{
                Source = Join-CheckedTreePath $From $Relative
                Relative = $Relative
                IsDirectory = $true
            }
        }
        foreach ($Relative in @($NewFiles.Values | Sort-Object)) {
            [PSCustomObject]@{
                Source = Join-CheckedTreePath $From $Relative
                Relative = $Relative
                IsDirectory = $false
            }
        }
    )
    foreach ($Entry in $Entries) {
        $Destination = Join-CheckedTreePath $To $Entry.Relative
        if ($Entry.IsDirectory) {
            if (Test-Path -LiteralPath $Destination -PathType Leaf) {
                throw "Program overlay type collision: $($Entry.Relative)"
            }
        }
        elseif (Test-Path -LiteralPath $Destination -PathType Container) {
            throw "Program overlay type collision: $($Entry.Relative)"
        }
    }
    foreach ($Entry in $Entries | Where-Object { $_.IsDirectory }) {
        $Destination = Join-CheckedTreePath $To $Entry.Relative
        if (-not (Test-Path -LiteralPath $Destination)) {
            [IO.Directory]::CreateDirectory($Destination) | Out-Null
            $script:NewDirectories += $Destination
        }
    }
    foreach ($Entry in $Entries | Where-Object { -not $_.IsDirectory }) {
        $Destination = Join-CheckedTreePath $To $Entry.Relative
        $BackupFile = Join-CheckedTreePath $Backup $Entry.Relative
        $Existed = Test-Path -LiteralPath $Destination -PathType Leaf
        if ($Existed) {
            [IO.Directory]::CreateDirectory((Split-Path -Parent $BackupFile)) | Out-Null
            [IO.File]::Copy($Destination, $BackupFile, $false)
        }
        $script:OverlayRecords += [PSCustomObject]@{
            Destination = $Destination; Backup = $BackupFile; Existed = $Existed
        }
        Copy-FileAtomically $Entry.Source $Destination
    }

    # Retire only paths proven by the previous release manifest to be program
    # files and the fixed compatibility allowlist.  Unknown target files,
    # device data and immutable version slots are outside this deletion set.
    $RetiredFiles = @{}
    foreach ($OldRelative in @($OldFiles.Values)) {
        if (-not $NewFiles.ContainsKey($OldRelative)) {
            $RetiredFiles[$OldRelative] = $OldRelative
        }
    }
    foreach ($ExplicitRetired in @("Enable-Power-Actions.bat")) {
        if (-not $NewFiles.ContainsKey($ExplicitRetired)) {
            $RetiredFiles[$ExplicitRetired] = $ExplicitRetired
        }
    }
    foreach ($RetiredRelative in @($RetiredFiles.Values | Sort-Object)) {
        $Destination = Join-CheckedTreePath $To $RetiredRelative
        if (-not (Test-Path -LiteralPath $Destination)) { continue }
        if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
            throw "Retired portable program path is not a file: $RetiredRelative"
        }
        Assert-PlainPath $Destination "Retired portable program file"
        $BackupFile = Join-CheckedTreePath $Backup $RetiredRelative
        [IO.Directory]::CreateDirectory((Split-Path -Parent $BackupFile)) | Out-Null
        [IO.File]::Copy($Destination, $BackupFile, $false)
        $script:OverlayRecords += [PSCustomObject]@{
            Destination = $Destination; Backup = $BackupFile; Existed = $true
        }
        Remove-Item -LiteralPath $Destination -Force
    }
}

function Undo-ProgramOverlay {
    $Records = @($script:OverlayRecords)
    [Array]::Reverse($Records)
    foreach ($Record in $Records) {
        if ($Record.Existed) {
            Copy-FileAtomically $Record.Backup $Record.Destination
        }
        elseif (Test-Path -LiteralPath $Record.Destination -PathType Leaf) {
            Remove-Item -LiteralPath $Record.Destination -Force
        }
    }
    foreach ($Directory in @($script:NewDirectories | Sort-Object Length -Descending)) {
        if ((Test-Path -LiteralPath $Directory -PathType Container) -and
            @([IO.Directory]::EnumerateFileSystemEntries($Directory)).Count -eq 0) {
            [IO.Directory]::Delete($Directory, $false)
        }
    }
}

function Remove-CheckedTree([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-PlainTree $Path
    [IO.Directory]::Delete($Path, $true)
}

function Start-AgentAsAdministrator([string]$Root) {
    $Script = Join-Path $Root "portable-agent.ps1"
    if (-not (Test-Path -LiteralPath $Script -PathType Leaf)) { return }
    $Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass' +
        ' -File "' + $Script + '" -Action Start -Scheduled'
    Start-Process -FilePath $PowerShellPath -ArgumentList $Arguments `
        -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
}

function Wait-AgentForRoot([string]$Root) {
    $Deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $Deadline) {
        $Processes = @(Get-AgentProcessesForRoot $Root)
        if ($Processes.Count -gt 1) {
            throw "More than one protected Agent process was started."
        }
        if ($Processes.Count -eq 1) {
            $ExpectedPid = [int]$Processes[0].Id
            Start-Sleep -Seconds 2
            $Stable = @(Get-AgentProcessesForRoot $Root | Where-Object { $_.Id -eq $ExpectedPid })
            if ($Stable.Count -eq 1) { return }
        }
        Start-Sleep -Milliseconds 500
    }
    throw "The protected Agent did not remain running after autostart installation."
}

function Write-MigrationOutcomeLog([string]$Outcome) {
    # Durable, bounded, secret-free migration trail beside the source data.
    # The parent launcher reprints this log's tail when the coordinator fails,
    # because the coordinator's own console output used to disappear with its
    # window before anyone could read the actual preflight reason.
    try {
        $LogDirectory = Join-Path (Join-Path $SourceRoot "data") "logs"
        [IO.Directory]::CreateDirectory($LogDirectory) | Out-Null
        $LogPath = Join-Path $LogDirectory "portable-migration.log"
        $Line = ([DateTime]::UtcNow.ToString("o")) + " " + $Outcome
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
        # Outcome logging must never mask the migration result itself.
    }
}

function Get-MigrationLogTail([string]$Path, [int]$LineCount) {
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

function Exit-MigrationMutex {
    if ($script:MigrationMutexAcquired -and $null -ne $script:MigrationMutex) {
        try { $script:MigrationMutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $script:MigrationMutex) {
        $script:MigrationMutex.Dispose()
    }
    $script:MigrationMutex = $null
    $script:MigrationMutexAcquired = $false
}

try {
    if (-not (Test-IsAdministrator)) {
        throw "Portable migration requires administrator approval."
    }
    $MigrationMutex = New-Object Threading.Mutex(
        $false,
        "Global\RigPilotAgentPortableMigrationV1"
    )
    try {
        $MigrationMutexAcquired = $MigrationMutex.WaitOne([TimeSpan]::Zero)
    }
    catch [Threading.AbandonedMutexException] {
        $MigrationMutexAcquired = $true
    }
    if (-not $MigrationMutexAcquired) {
        throw "Another portable migration is already running."
    }
    if ([string]::Equals($SourceRoot, $TargetRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The migration source is already the protected portable root."
    }
    Assert-PlainPathComponents $SourceRoot "Portable source path"
    Assert-PlainPathComponents $ProgramDataRoot "ProgramData path"
    Assert-PlainTree $SourceRoot
    foreach ($Required in @(
        "RigPilotAgent.exe", "portable-agent.ps1", "portable-migrate.ps1",
        "RigPilotAgentUpdater.ps1"
    )) {
        Assert-PlainPath (Join-Path $SourceRoot $Required) "Required portable component"
    }
    Assert-PortableBundleManifest $SourceRoot
    $SourceIdentity = Read-EnrollmentIdentity $SourceRoot
    $SourceMinerWasRunning = Test-LiveManagedMiner $SourceRoot

    Import-Module ScheduledTasks -ErrorAction Stop
    $SourceTask = Get-CheckedTaskForRoot $SourceRoot
    $TargetExisted = Test-Path -LiteralPath $TargetRoot -PathType Container
    if (Test-Path -LiteralPath $TargetRoot -PathType Leaf) {
        throw "The protected portable destination is occupied by a file."
    }
    if ($TargetExisted) {
        Assert-PlainPathComponents $TargetRoot "Portable target path"
        Assert-PlainTree $TargetRoot
        $TargetIdentity = Read-EnrollmentIdentity $TargetRoot
        if (-not [string]::Equals($SourceIdentity.AgentId, $TargetIdentity.AgentId,
                [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($SourceIdentity.AgentSecret, $TargetIdentity.AgentSecret,
                [StringComparison]::Ordinal)) {
            throw "The protected destination belongs to a different device identity. Nothing was changed."
        }
        $TargetMinerWasRunning = Test-LiveManagedMiner $TargetRoot
        if ($SourceMinerWasRunning -and $TargetMinerWasRunning) {
            throw "Both the source and target report a live miner; migration cannot choose one safely."
        }
        if ($SourceMinerWasRunning) {
            throw "The source miner is live while a protected same-identity target already exists; stop mining and retry after reviewing the target."
        }
    }
    $TargetTask = Get-CheckedTaskForRoot $TargetRoot
    $TargetTaskExisted = $null -ne $TargetTask
    $TargetRecoveryTask = Get-CheckedTaskForRoot $TargetRoot $true
    $TargetRecoveryTaskExisted = $null -ne $TargetRecoveryTask

    $SourceWasRunning = @(Get-AgentProcessesForRoot $SourceRoot).Count -gt 0
    if ($TargetExisted) {
        $TargetWasRunning = @(Get-AgentProcessesForRoot $TargetRoot).Count -gt 0
    }
    # All source, identity, task and mutex checks above are read-only. A failed
    # preflight must never stop, unregister or remove an existing installation.
    $MutationStarted = $true
    $SourceAgentStopAttempted = $true
    Stop-AgentForRoot $SourceRoot | Out-Null
    if ($TargetExisted) {
        $TargetAgentStopAttempted = $true
        Stop-AgentForRoot $TargetRoot | Out-Null
    }
    $OriginalMiningRoot = if ($TargetMinerWasRunning) { $TargetRoot } else { $SourceRoot }
    if ($SourceMinerWasRunning -or $TargetMinerWasRunning) {
        $MinerStopAttempted = $true
        Invoke-ManagedMinerOnce $SourceRoot $OriginalMiningRoot "Stop"
        if (Test-LiveManagedMiner $OriginalMiningRoot) {
            throw "The managed miner is still running after the verified stop request."
        }
    }

    $Nonce = [Guid]::NewGuid().ToString("N")
    if (-not $TargetExisted) {
        $StagingRoot = Join-Path $ProgramDataRoot ("RigPilotAgentPortable.migrate-" + $Nonce)
        New-RestrictedDirectory $StagingRoot
        Copy-PlainTree $SourceRoot $StagingRoot $false
        foreach ($Transient in @(
            "agent.pid.json", "agent-update-ready.json", "updater-ready.json",
            "active-agent.json"
        )) {
            $TransientPath = Join-Path (Join-Path $StagingRoot "data") $Transient
            if (Test-Path -LiteralPath $TransientPath -PathType Leaf) {
                Remove-Item -LiteralPath $TransientPath -Force
            }
        }
        $PendingUpdates = Join-Path (Join-Path $StagingRoot "data") "agent-updates"
        if (Test-Path -LiteralPath $PendingUpdates -PathType Container) {
            Remove-CheckedTree $PendingUpdates
        }
        Remove-UnmanifestedProgramFiles $StagingRoot
        Assert-PlainTree $StagingRoot
        Assert-PortableBundleManifest $StagingRoot
        [IO.Directory]::Move($StagingRoot, $TargetRoot)
        $TargetCreated = $true
        $StagingRoot = $null
    }
    else {
        $BackupRoot = Join-Path $ProgramDataRoot ("RigPilotAgentPortable.rollback-" + $Nonce)
        New-RestrictedDirectory $BackupRoot
        $TargetOverlayStarted = $true
        Apply-ProgramOverlay $SourceRoot $TargetRoot $BackupRoot
        Assert-PortableBundleManifest $TargetRoot
    }

    if (-not $TargetExisted) {
        Update-MigratedDataPaths $SourceRoot $TargetRoot
    }
    $MigratedIdentity = Read-EnrollmentIdentity $TargetRoot
    if (-not [string]::Equals($SourceIdentity.AgentId, $MigratedIdentity.AgentId,
            [StringComparison]::OrdinalIgnoreCase) -or
        -not [string]::Equals($SourceIdentity.AgentSecret, $MigratedIdentity.AgentSecret,
            [StringComparison]::Ordinal)) {
        throw "The migrated device identity changed during copy; migration was rolled back."
    }

    if ($SourceMinerWasRunning -or $TargetMinerWasRunning) {
        # A failing resume may already have started the verified miner.
        $TargetMinerStarted = $true
        Invoke-ManagedMinerOnce $TargetRoot $TargetRoot "Resume"
        if (-not (Test-LiveManagedMiner $TargetRoot)) {
            throw "The managed miner did not restart from the protected target path."
        }
    }

    # Only an exact, fully-owned legacy task is removed, and it is removed
    # before the protected target registers its replacement.  It is never
    # recreated on rollback because that would restore SYSTEM execution from
    # a user-writable directory.
    if ($null -ne $SourceTask) {
        Unregister-ScheduledTask -TaskName $SourceTask.TaskName -TaskPath "\" -Confirm:$false
        $SourceTaskRemoved = $true
    }

    $TargetScript = Join-Path $TargetRoot "portable-agent.ps1"
    $InstallArguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        $TargetScript + '" -Action InstallAutostart -MigrationInstall'
    $TargetTaskInstallAttempted = $true
    # -NoNewWindow keeps the target install's console output visible here; its
    # launcher also appends the failure to the durable protected-root log.
    $Install = Start-Process -FilePath $PowerShellPath -ArgumentList $InstallArguments `
        -WorkingDirectory $TargetRoot -NoNewWindow -Wait -PassThru
    $Install.Refresh()
    if ([int]$Install.ExitCode -ne 0) {
        $TargetLauncherLog = Join-Path (Join-Path $TargetRoot "data") "logs\portable-launcher.log"
        $TargetTail = Get-MigrationLogTail $TargetLauncherLog 6
        $TargetDetail = if ($TargetTail) { " Target launcher log: $TargetTail" } else { "" }
        throw "The protected target could not install autostart (exit $($Install.ExitCode)).$TargetDetail"
    }
    $VerifiedTargetTask = Get-CheckedTaskForRoot $TargetRoot
    if ($null -eq $VerifiedTargetTask) {
        throw "The protected target autostart task was not registered."
    }
    $ReadyMarker = Join-Path (Join-Path $TargetRoot "data") "agent-update-ready.json"
    if (-not (Test-Path -LiteralPath $ReadyMarker -PathType Leaf)) {
        throw "The protected target did not enable authenticated Agent updates."
    }
    Wait-AgentForRoot $TargetRoot

    if ($null -ne $BackupRoot) {
        try {
            Remove-CheckedTree $BackupRoot
            $BackupRoot = $null
        }
        catch {
            # The replacement is already healthy. Cleanup may have removed
            # only part of the backup, so it cannot trigger a safe rollback.
            Write-Warning "Migration succeeded; old program backup cleanup failed: $BackupRoot; $($_.Exception.Message)"
        }
    }
    Write-Host "RigPilot Agent migrated to: $TargetRoot" -ForegroundColor Green
    Write-Host "Program, config, identity, and data were preserved. Retire the old copy after verifying migration."
    Write-Host "Only the protected ProgramData copy is registered for SYSTEM autostart."
    Write-MigrationOutcomeLog ("succeeded: migrated to " + $TargetRoot)
    Exit-MigrationMutex
    exit 0
}
catch {
    $Failure = $_.Exception.Message
    $RollbackSucceeded = $true
    if ($MutationStarted) {
        try {
            if (($TargetCreated -or $TargetOverlayStarted -or $TargetTaskInstallAttempted) -and
                (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
                Stop-AgentForRoot $TargetRoot | Out-Null
            }
            if ($TargetMinerStarted -and (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
                Invoke-ManagedMinerOnce $SourceRoot $TargetRoot "Stop"
                $TargetMinerStarted = $false
            }
            if ($TargetTaskInstallAttempted -and -not $TargetTaskExisted) {
                $CurrentTargetTask = $null
                try { $CurrentTargetTask = Get-CheckedTaskForRoot $TargetRoot } catch {}
                if ($null -ne $CurrentTargetTask) {
                    Unregister-ScheduledTask -TaskName $CurrentTargetTask.TaskName `
                        -TaskPath "\" -Confirm:$false
                }
            }
            if ($TargetTaskInstallAttempted -and -not $TargetRecoveryTaskExisted) {
                $CurrentRecoveryTask = $null
                try { $CurrentRecoveryTask = Get-CheckedTaskForRoot $TargetRoot $true } catch {}
                if ($null -ne $CurrentRecoveryTask) {
                    Unregister-ScheduledTask -TaskName $CurrentRecoveryTask.TaskName `
                        -TaskPath "\" -Confirm:$false
                }
            }
            if ($TargetOverlayStarted) {
                Undo-ProgramOverlay
            }
            elseif ($TargetCreated -and (Test-Path -LiteralPath $TargetRoot -PathType Container)) {
                Remove-CheckedTree $TargetRoot
            }
            if ($MinerStopAttempted) {
                $RestoreMiningRoot = if ($TargetMinerWasRunning) { $TargetRoot } else { $SourceRoot }
                Invoke-ManagedMinerOnce $SourceRoot $RestoreMiningRoot "Resume"
            }
            if ($SourceAgentStopAttempted -and $SourceWasRunning) { Start-AgentAsAdministrator $SourceRoot }
            if ($TargetAgentStopAttempted -and $TargetWasRunning) { Start-AgentAsAdministrator $TargetRoot }
        }
        catch {
            $RollbackSucceeded = $false
            Write-Host "ROLLBACK WARNING: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
    foreach ($TemporaryRoot in $OwnedTemporaryRoots) {
        if (-not $RollbackSucceeded -and $TemporaryRoot -eq $BackupRoot) {
            Write-Warning "Incomplete rollback; original program backup retained: $BackupRoot"
            continue
        }
        if (-not [string]::IsNullOrWhiteSpace([string]$TemporaryRoot)) {
            try { Remove-CheckedTree $TemporaryRoot } catch {}
        }
    }
    if ($SourceTaskRemoved) {
        Write-Host "The unsafe legacy SYSTEM task was not recreated; retry migration after fixing the error." -ForegroundColor Yellow
    }
    Write-MigrationOutcomeLog ("failed: " + $Failure)
    Write-Host "ERROR: $Failure" -ForegroundColor Red
    Write-Host "The old portable folder and its data were kept."
    Write-Host "The migration reason was also appended to data\logs\portable-migration.log in the old folder."
    Exit-MigrationMutex
    exit 1
}
