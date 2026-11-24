[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$DotNetPath,
    [string]$OutputDir,
    [switch]$SkipTests,
    [switch]$SkipWindows,
    [switch]$SkipController,
    # Compatibility with callers that already selected Windows-only builds.
    [switch]$SkipLinux
)

$ErrorActionPreference = "Stop"
$VersionPattern = '^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Run this entry point in the Windows build VM. Use build-all-releases.sh in the Ubuntu build environment."
}
if ($PSBoundParameters.ContainsKey("SkipLinux") -and -not $SkipLinux) {
    throw "Linux releases are built separately in Ubuntu; this Windows entry point cannot include them."
}
if ($SkipWindows -and $SkipController) {
    throw "No local release target selected."
}

$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $ProjectRoot "outputs"
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
$AgentVersion = ""
$ControllerVersion = ""

Push-Location -LiteralPath $ProjectRoot
try {
    if (-not $SkipWindows) {
        $AgentVersion = (& $PythonPath -c "from agent.version import __version__; print(__version__)").Trim()
        if ($LASTEXITCODE -ne 0 -or $AgentVersion -notmatch $VersionPattern) {
            throw "Could not read the Agent source version."
        }
    }
    if (-not $SkipController) {
        $ControllerVersion = (& $PythonPath -c "from server import __version__; print(__version__)").Trim()
        if ($LASTEXITCODE -ne 0 -or $ControllerVersion -notmatch $VersionPattern) {
            throw "Could not read the Controller source version."
        }
    }
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
    $Platforms = @()
    if (-not $SkipController) {
        & $PythonPath (Join-Path $PSScriptRoot "build-controller-release.py") `
            --version $ControllerVersion --output-dir $OutputDir
        if ($LASTEXITCODE -ne 0) { throw "Controller release build failed." }
        $Platforms += "controller"
    }
    if (-not $SkipWindows) {
        & (Join-Path $PSScriptRoot "windows\build-windows-portable.ps1") `
            -PythonPath $PythonPath -DotNetPath $DotNetPath `
            -OutputDir $OutputDir -ReleaseVersion $AgentVersion -SkipTests:$SkipTests
        if ($LASTEXITCODE -ne 0) { throw "Windows Agent release build failed." }
        $Platforms += "windows"
    }

    $VerifyArguments = @(
        (Join-Path $PSScriptRoot "verify-release-artifacts.py"),
        "--output-dir", $OutputDir,
        "--skip-linux"
    )
    if ($SkipWindows) { $VerifyArguments += "--skip-windows" }
    else { $VerifyArguments += @("--agent-version", $AgentVersion) }
    if ($SkipController) { $VerifyArguments += "--skip-controller" }
    else { $VerifyArguments += @("--controller-version", $ControllerVersion) }
    if (-not $SkipWindows) { $VerifyArguments += @("--smoke-native", "windows") }
    & $PythonPath @VerifyArguments
    if ($LASTEXITCODE -ne 0) { throw "Local release artifact verification failed." }

    Write-Host "BUILD_ONLY=1"
    Write-Host "BUILD_PLATFORMS=$($Platforms -join ',')"
    Write-Host "OUTPUT_DIR=$OutputDir"
    Write-Host "AGENT_VERSION=$AgentVersion"
    Write-Host "CONTROLLER_VERSION=$ControllerVersion"
}
finally {
    Pop-Location
}
