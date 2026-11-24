[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Start", "Stop", "InstallAutostart", "RemoveAutostart", "CheckConnection")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$PortableRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$EntryPath = [IO.Path]::GetFullPath($script:PSCommandPath)
$LauncherPath = Join-Path $PortableRoot "portable-agent.ps1"
$PowerShellPath = Join-Path ([Environment]::SystemDirectory) `
    "WindowsPowerShell\v1.0\powershell.exe"

function Assert-PlainEntryPath([string]$Path, [string]$Description, [bool]$Directory) {
    $ExpectedType = if ($Directory) { "Container" } else { "Leaf" }
    if (-not (Test-Path -LiteralPath $Path -PathType $ExpectedType)) {
        throw "$Description is missing. Extract the complete portable ZIP before running."
    }
    $Item = Get-Item -LiteralPath $Path -Force
    if (($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description must not be a symbolic link or junction."
    }
}

function Test-IsAdministrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    return $Principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

try {
    Assert-PlainEntryPath $PortableRoot "Portable directory" $true
    Assert-PlainEntryPath $EntryPath "Portable compatibility entry" $false
    Assert-PlainEntryPath $LauncherPath "Portable launcher" $false
    if (-not (Test-Path -LiteralPath $PowerShellPath -PathType Leaf)) {
        throw "Windows PowerShell 5.1 is unavailable."
    }

    # The trusted launcher may be an older immutable copy. Elevate that fixed
    # launcher before it can resolve any Administrators/SYSTEM-only state. The
    # mutable compatibility entry itself never receives an administrator token.
    if (-not (Test-IsAdministrator)) {
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
        if ($LauncherPath.Contains('"')) {
            throw "Portable launcher path contains an unsupported quote character."
        }
        # Windows PowerShell 5.1 joins a string[] ambiguously for paths with
        # spaces. Pass one fixed command line; Action is constrained above and
        # Windows paths cannot contain a double quote.
        $ElevationArguments = '-NoProfile -ExecutionPolicy Bypass -File "' +
            $LauncherPath + '" -Action ' + $Action
        $Elevated = Start-Process `
            -FilePath $PowerShellPath `
            -ArgumentList $ElevationArguments `
            -WorkingDirectory $PortableRoot `
            -Verb RunAs `
            -Wait `
            -PassThru
        if ($null -eq $Elevated) {
            throw "Administrator approval was not granted."
        }
        $Elevated.Refresh()
        exit [int]$Elevated.ExitCode
    }

    # Use only the legacy action interface shared by every update-capable
    # trusted launcher. Scheduled SYSTEM startup continues to invoke the
    # launcher directly with -Scheduled and never passes through this helper.
    & $PowerShellPath `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $LauncherPath `
        -Action $Action
    exit [int]$LASTEXITCODE
}
catch [ComponentModel.Win32Exception] {
    if ([int]$_.Exception.NativeErrorCode -eq 1223) {
        Write-Error "Administrator approval was cancelled."
        exit 1223
    }
    Write-Error $_.Exception.Message
    exit 1
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
