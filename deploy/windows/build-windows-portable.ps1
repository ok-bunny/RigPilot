[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$DotNetPath,
    [string]$OutputDir,
    [string]$ReleaseVersion,
    [switch]$SkipTests,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.IO.Compression
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$WorkParent = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "work"))
$WorkName = "windows-portable.$PID.$([Guid]::NewGuid().ToString('N'))"
$WorkRoot = [IO.Path]::GetFullPath((Join-Path $WorkParent $WorkName))
$ExpectedWorkPrefix = $WorkParent.TrimEnd('\') + '\'
if (-not $WorkRoot.StartsWith($ExpectedWorkPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "便携版构建目录必须位于 workspace\work 下。"
}

if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
$BootstrapPythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $ProjectRoot "outputs"
}
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
New-Item -ItemType Directory -Path $WorkParent -Force | Out-Null

function Initialize-DotNet8BuildScope([string]$CommandPath, [string]$ScopeRoot) {
    $InstalledSdks = @(& $CommandPath --list-sdks 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取已安装的 .NET SDK。"
    }
    $SdkVersions = @(
        foreach ($SdkLine in $InstalledSdks) {
            if ([string]$SdkLine -match '^(8\.0\.[0-9]+)\s+\[') {
                [version]$Matches[1]
            }
        }
    )
    if ($SdkVersions.Count -eq 0) {
        throw "CPU 温度辅助组件需要已安装的正式 .NET 8 SDK。"
    }
    $SelectedSdkVersion = ($SdkVersions | Sort-Object -Descending | Select-Object -First 1).ToString()
    $SdkSelection = [ordered]@{
        sdk = [ordered]@{
            version = $SelectedSdkVersion
            rollForward = "disable"
            allowPrerelease = $false
        }
    }
    [IO.File]::WriteAllText(
        (Join-Path $ScopeRoot "global.json"),
        (($SdkSelection | ConvertTo-Json -Depth 3) + [Environment]::NewLine),
        (New-Object Text.UTF8Encoding($false))
    )
    Push-Location -LiteralPath $ScopeRoot
    try {
        $DotNetVersionOutput = @(& $CommandPath --version 2>$null)
        $DotNetVersionExitCode = $LASTEXITCODE
        $DotNetSdkVersion = if ($DotNetVersionOutput.Count -gt 0) {
            ([string]$DotNetVersionOutput[0]).Trim()
        } else { "" }
        if ($DotNetVersionExitCode -ne 0 -or $DotNetVersionOutput.Count -ne 1 -or
            $DotNetSdkVersion -cne $SelectedSdkVersion) {
            throw "CPU 温度辅助组件未使用选定的 .NET 8 SDK：$DotNetSdkVersion"
        }
    }
    finally {
        Pop-Location
    }
    return $SelectedSdkVersion
}

if (Test-Path -LiteralPath $WorkRoot) {
    throw "随机发布构建目录发生冲突，拒绝复用：$WorkRoot"
}
New-Item -ItemType Directory -Path $WorkRoot | Out-Null
try {

if (-not $DotNetPath) {
    $WorkspaceSdk = Join-Path $WorkParent "dotnet-sdk-8.0.422\dotnet.exe"
    if (Test-Path -LiteralPath $WorkspaceSdk -PathType Leaf) {
        $DotNetPath = $WorkspaceSdk
    }
    else {
        $DotNetCommand = Get-Command dotnet.exe -ErrorAction SilentlyContinue
        if ($null -ne $DotNetCommand) {
            $DotNetPath = $DotNetCommand.Source
        }
    }
}
if (-not $DotNetPath -or -not (Test-Path -LiteralPath $DotNetPath -PathType Leaf)) {
    throw "构建 0.6.15+ Windows 便携包需要 .NET 8 SDK（用于 CPU 温度辅助组件）。"
}
$DotNetPath = (Resolve-Path -LiteralPath $DotNetPath).Path
$DotNetSdkVersion = Initialize-DotNet8BuildScope $DotNetPath $WorkRoot

# Build and test inside one release-scoped environment.  The caller's Python
# only bootstraps it and is never mutated by package construction.
$BuildVenv = Join-Path $WorkRoot "venv"
& $BootstrapPythonPath -m venv $BuildVenv
if ($LASTEXITCODE -ne 0) {
    throw "无法创建隔离的 Windows 发布构建环境。"
}
$PythonPath = Join-Path $BuildVenv "Scripts\python.exe"
# Validate the interpreter that actually installs, tests, and freezes the
# release. ARM64 Python is also 64-bit, so pointer size is not sufficient.
$BuildPlatformOutput = @(& $PythonPath -c "import sysconfig; print(sysconfig.get_platform())")
if ($LASTEXITCODE -ne 0 -or $BuildPlatformOutput.Count -ne 1 -or
    ([string]$BuildPlatformOutput[0]).Trim() -cne "win-amd64") {
    throw "Windows x64 packages require an AMD64 Python interpreter (win-amd64); ARM64 Python is not a supported build interpreter."
}
& $PythonPath -m pip install --disable-pip-version-check `
    -r (Join-Path $ProjectRoot "requirements-dev.txt") `
    -r (Join-Path $PSScriptRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "无法安装精确锁定的 Windows 发布依赖。"
}
& $PythonPath -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "Windows 发布构建环境依赖检查失败。"
}

$BuildProjectRoot = $ProjectRoot
$AgentEntryPoint = Join-Path $ProjectRoot "deploy\windows_agent_entry.py"
$CurrentSourceVersion = (& $PythonPath -c "from agent.version import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or $CurrentSourceVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "无法读取当前 Agent 源码版本。"
}
if (-not $ReleaseVersion) {
    $ReleaseVersion = $CurrentSourceVersion
}
if ($ReleaseVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "发布版本必须是 X.Y.Z。"
}
if ($ReleaseVersion -ne $CurrentSourceVersion -and $ReleaseVersion -ne "0.5.6") {
    throw "拒绝把当前 Agent 源码伪装为历史版本 $ReleaseVersion；只允许当前源码版本 $CurrentSourceVersion 或明确的 0.5.6 更新过渡包。"
}
if ($ReleaseVersion -eq "0.5.6") {
    # 0.5.6 is the sole reviewed compatibility exception.  Build it from an
    # isolated mirror so the tracked Agent version is never rewritten.
    $BuildProjectRoot = Join-Path $WorkRoot "bootstrap-source"
    New-Item -ItemType Directory -Path $BuildProjectRoot | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "agent") `
        -Destination (Join-Path $BuildProjectRoot "agent") -Recurse
    # The current Agent imports the shared declarative miner adapter catalog.
    # Keep the historical bootstrap build isolated, but copy the exact audited
    # package it needs so PyInstaller cannot silently omit the trust contract.
    New-Item -ItemType Directory -Path (Join-Path $BuildProjectRoot "server") | Out-Null
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "server\__init__.py") `
        -Destination (Join-Path $BuildProjectRoot "server\__init__.py")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "server\pci_identity.py") `
        -Destination (Join-Path $BuildProjectRoot "server\pci_identity.py")
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "server\miner_adapters") `
        -Destination (Join-Path $BuildProjectRoot "server\miner_adapters") -Recurse
    New-Item -ItemType Directory -Path (Join-Path $BuildProjectRoot "deploy") | Out-Null
    $AgentEntryPoint = Join-Path $BuildProjectRoot "deploy\windows_agent_entry.py"
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "deploy\windows_agent_entry.py") `
        -Destination $AgentEntryPoint
    $VersionSource = Join-Path $BuildProjectRoot "agent\version.py"
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText(
        $VersionSource,
        "`"`"`"Agent version information.`"`"`"`r`n`r`n__version__ = `"$ReleaseVersion`"`r`n",
        $Utf8NoBom
    )
}

if (-not $SkipTests) {
    $PytestBaseTemp = Join-Path $WorkRoot "pytest"
    # The Windows release gate exercises every shared Agent test, including
    # miner packages and both first-contact journals. Linux-only tests have
    # their own native release gate and must not run against Windows systemd.
    $AgentTestFiles = @(
        Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "agent\tests") -Filter "test_*.py" -File |
            Where-Object { $_.Name -notmatch '^(test_hiveos_|test_linux_)' } |
            Sort-Object Name |
            ForEach-Object { "agent/tests/$($_.Name)" }
    )
    $WindowsContractTests = @(
        "tests/test_agent_enrollment_recovery.py",
        "tests/test_device_replacement_and_settlement.py",
        "tests/test_windows_installer.py",
        "tests/test_windows_portable.py",
        "tests/test_windows_portable_enrollment.py"
    )
    $SelectedTests = @($AgentTestFiles) + @($WindowsContractTests)
    if ($AgentTestFiles.Count -eq 0 -or
        $AgentTestFiles -notcontains "agent/tests/test_miner_packages.py" -or
        $AgentTestFiles -notcontains "agent/tests/test_pending_replacement.py") {
        throw "Windows Agent test discovery omitted required shared tests."
    }
    foreach ($RelativeTest in $SelectedTests) {
        $TestFile = Join-Path $ProjectRoot ($RelativeTest.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $TestFile -PathType Leaf)) {
            throw "Windows Agent test file is missing: $RelativeTest"
        }
    }
    Write-Host "Windows Agent release test files ($($SelectedTests.Count)):"
    foreach ($RelativeTest in $SelectedTests) {
        Write-Host "  $RelativeTest"
    }
    Push-Location $ProjectRoot
    try {
        & $PythonPath -m pytest -q --basetemp $PytestBaseTemp @SelectedTests
        $TestExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
    if ($TestExitCode -ne 0) {
        throw "测试失败，停止便携版构建。"
    }
}

$DistRoot = Join-Path $WorkRoot "dist"
$PyInstallerWork = Join-Path $WorkRoot "pyinstaller"
$SpecRoot = Join-Path $WorkRoot "spec"
& $PythonPath -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --noupx `
    --name RigPilotAgent `
    --paths $BuildProjectRoot `
    --distpath $DistRoot `
    --workpath $PyInstallerWork `
    --specpath $SpecRoot `
    $AgentEntryPoint
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 便携版构建失败。"
}

$BundleDir = Join-Path $DistRoot "RigPilotAgent"
$AgentExe = Join-Path $BundleDir "RigPilotAgent.exe"
if (-not (Test-Path -LiteralPath $AgentExe -PathType Leaf)) {
    throw "冻结后的便携版 Agent EXE 不存在。"
}
# Validate the actual frozen binary before running it or labeling the manifest
# x86_64. A working emulated binary alone does not establish its architecture.
$AgentHeader = [IO.File]::ReadAllBytes($AgentExe)
if ($AgentHeader.Length -lt 64 -or $AgentHeader[0] -ne 0x4D -or $AgentHeader[1] -ne 0x5A) {
    throw "Frozen Agent is not a valid Windows PE executable."
}
$AgentPeOffset = [BitConverter]::ToInt32($AgentHeader, 0x3C)
if ($AgentPeOffset -lt 64 -or $AgentPeOffset -gt ($AgentHeader.Length - 26) -or
    [BitConverter]::ToUInt32($AgentHeader, $AgentPeOffset) -ne 0x00004550 -or
    [BitConverter]::ToUInt16($AgentHeader, $AgentPeOffset + 4) -ne 0x8664 -or
    [BitConverter]::ToUInt16($AgentHeader, $AgentPeOffset + 24) -ne 0x020B) {
    throw "Frozen Agent must be an AMD64 PE32+ executable; refusing to publish a mislabeled x64 package."
}
$VersionOutput = & $AgentExe --version
$ExpectedVersionOutput = "RigPilot Agent $ReleaseVersion"
if ($LASTEXITCODE -ne 0 -or $VersionOutput.Trim() -ne $ExpectedVersionOutput) {
    throw "冻结后的便携版 Agent 版本自检失败：$VersionOutput"
}

$CpuTemperatureSource = Join-Path $PSScriptRoot "cpu-temperature-helper"
$CpuTemperatureBuildSource = Join-Path $WorkRoot "helper-source"
New-Item -ItemType Directory -Path $CpuTemperatureBuildSource | Out-Null
# Both CLI and project SDK resolution must find this release's global.json.
# Keep obj/bin writes in the owned work tree and copy only declared inputs.
foreach ($InputName in @("RigPilot.CpuTemperature.csproj", "Program.cs", "packages.lock.json")) {
    [IO.File]::Copy(
        (Join-Path $CpuTemperatureSource $InputName),
        (Join-Path $CpuTemperatureBuildSource $InputName),
        $false
    )
}
$CpuTemperatureProject = Join-Path $CpuTemperatureBuildSource "RigPilot.CpuTemperature.csproj"
$CpuTemperaturePublish = Join-Path $WorkRoot "cpu-temperature-helper"
Push-Location -LiteralPath $WorkRoot
try {
    & $DotNetPath restore $CpuTemperatureProject --locked-mode --nologo
    if ($LASTEXITCODE -ne 0) {
        throw "无法按锁定清单恢复 Windows CPU 温度辅助组件。"
    }
    & $DotNetPath publish $CpuTemperatureProject `
        -c Release `
        -r win-x64 `
        --self-contained false `
        --no-restore `
        -o $CpuTemperaturePublish `
        --nologo
    if ($LASTEXITCODE -ne 0) {
        throw "Windows CPU 温度辅助组件构建失败。"
    }
}
finally {
    Pop-Location
}
$CpuTemperatureExe = Join-Path $CpuTemperaturePublish "RigPilot.CpuTemperature.exe"
if (-not (Test-Path -LiteralPath $CpuTemperatureExe -PathType Leaf)) {
    throw "Windows CPU 温度辅助组件 EXE 不存在。"
}
$CpuTemperatureVersion = (& $CpuTemperatureExe --version).Trim()
if ($LASTEXITCODE -ne 0 -or $CpuTemperatureVersion -ne "RigPilot CPU Temperature Helper 1.0.0 (LibreHardwareMonitor 0.9.6)") {
    throw "Windows CPU 温度辅助组件版本自检失败：$CpuTemperatureVersion"
}
$CpuTemperatureBundle = Join-Path $BundleDir "tools\cpu-temperature"
New-Item -ItemType Directory -Path $CpuTemperatureBundle -Force | Out-Null
foreach ($CpuTemperatureItem in @(Get-ChildItem -LiteralPath $CpuTemperaturePublish -File)) {
    if ($CpuTemperatureItem.Extension -notin @(".pdb", ".xml")) {
        Copy-Item -LiteralPath $CpuTemperatureItem.FullName `
            -Destination (Join-Path $CpuTemperatureBundle $CpuTemperatureItem.Name) -Force
    }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "cpu-temperature-helper\THIRD-PARTY-NOTICES.txt") `
    -Destination (Join-Path $CpuTemperatureBundle "THIRD-PARTY-NOTICES.txt") -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "cpu-temperature-helper\packages.lock.json") `
    -Destination (Join-Path $CpuTemperatureBundle "packages.lock.json") -Force
$CpuTemperatureLicenses = Join-Path $PSScriptRoot "cpu-temperature-helper\LICENSES"
if (-not (Test-Path -LiteralPath $CpuTemperatureLicenses -PathType Container)) {
    throw "缺少 Windows CPU 温度辅助组件完整许可证目录。"
}
Copy-Item -LiteralPath $CpuTemperatureLicenses `
    -Destination (Join-Path $CpuTemperatureBundle "LICENSES") -Recurse -Force
if (-not $SkipSmokeTest) {
    & powershell.exe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "smoke-test-frozen-agent.ps1") `
        -AgentExePath $AgentExe `
        -PythonPath $PythonPath `
        -ProjectRoot $ProjectRoot `
        -ExpectedVersion $ReleaseVersion `
        -Port 18082
    if ($LASTEXITCODE -ne 0) {
        throw "冻结后的便携版 Agent 冒烟测试失败。"
    }
}

$PortableRoot = Join-Path $WorkRoot "RigPilotAgent-Portable"
New-Item -ItemType Directory -Path $PortableRoot | Out-Null
Copy-Item -Path (Join-Path $BundleDir "*") -Destination $PortableRoot -Recurse -Force
$PortableAssets = Join-Path $PSScriptRoot "portable"
foreach ($AssetName in @(
    "portable-entry.ps1",
    "portable-agent.ps1",
    "portable-migrate.ps1",
    "RigPilotAgentUpdater.ps1",
    "Start-RigPilotAgent.bat",
    "Stop-RigPilotAgent.bat",
    "Install-Autostart.bat",
    "Remove-Autostart.bat",
    "Check-Connection.bat",
    "README-Portable.txt"
)) {
    $AssetPath = Join-Path $PortableAssets $AssetName
    if (-not (Test-Path -LiteralPath $AssetPath -PathType Leaf)) {
        throw "缺少便携版资源：$AssetName"
    }
    Copy-Item -LiteralPath $AssetPath -Destination (Join-Path $PortableRoot $AssetName) -Force
}
$ThirdPartyNotices = Join-Path $PSScriptRoot "ThirdPartyNotices.txt"
if (-not (Test-Path -LiteralPath $ThirdPartyNotices -PathType Leaf)) {
    throw "缺少第三方许可声明：ThirdPartyNotices.txt"
}
Copy-Item -LiteralPath $ThirdPartyNotices -Destination (Join-Path $PortableRoot "ThirdPartyNotices.txt") -Force

if ($ReleaseVersion -eq "0.5.6") {
    $BootstrapWarning = @"
RIGPILOT UPDATE BOOTSTRAP - NOT AN OFFICIAL RELEASE

This build reports Agent 0.5.6 but contains only the agent_update.v1 bootstrap
needed to move one protected Windows portable device to a later official release.
Do not upload this ZIP to the controller release repository or distribute it as
an Agent release.  Overlay its RigPilotAgent-Portable contents onto the enrolled
legacy portable folder without deleting data, then run Install-Autostart.bat
once.  It migrates identity, config, data, and packages automatically into
C:\ProgramData\RigPilotAgentPortable and registers only that protected copy.
"@
    [IO.File]::WriteAllText(
        (Join-Path $PortableRoot "BOOTSTRAP-ONLY.txt"),
        $BootstrapWarning,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

# Bind every executable/support file to the release archive SHA.  The Agent
# requires an exact file-set match and rechecks each digest before it launches
# the fixed updater from the already-installed version.
$ManifestFiles = [ordered]@{}
foreach ($Item in @(Get-ChildItem -LiteralPath $PortableRoot -Recurse -File | Sort-Object FullName)) {
    $Relative = $Item.FullName.Substring($PortableRoot.Length + 1).Replace('\', '/')
    $ManifestFiles[$Relative] = (Get-FileHash -LiteralPath $Item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$Manifest = [ordered]@{
    schema_version = 1
    version = $ReleaseVersion
    platform = "windows"
    architecture = "x86_64"
    files = $ManifestFiles
}
$ManifestPath = Join-Path $PortableRoot "agent-release.json"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText(
    $ManifestPath,
    (($Manifest | ConvertTo-Json -Depth 8) + [Environment]::NewLine),
    $Utf8NoBom
)

if ($ReleaseVersion -eq "0.5.6") {
    $ArchiveName = "RigPilotAgent-0.5.6-to-0.6.0-UpdateBootstrap-Windows-x64.zip"
}
else {
    $ArchiveName = "RigPilotAgent-$ReleaseVersion-Windows-x64-Portable.zip"
}
$ZipPath = Join-Path $OutputDir $ArchiveName
if ((Test-Path -LiteralPath $ZipPath) -or (Test-Path -LiteralPath "$ZipPath.sha256")) {
    throw "拒绝覆盖已有 Windows 发布物；请使用空输出目录或先人工归档旧版本：$ZipPath"
}
$ZipStage = "$ZipPath.part.$PID"
$ZipHashPath = "$ZipPath.sha256"
$ZipHashStage = "$ZipHashPath.part.$PID"
Remove-Item -LiteralPath $ZipStage, $ZipHashStage -Force -ErrorAction SilentlyContinue
# Compress-Archive stores Windows backslashes in central-directory entry names.
# The self-updater intentionally rejects those ambiguous names, so write the
# portable archive explicitly with canonical ZIP forward slashes.  Keep one
# fixed top-level directory so manual extraction remains tidy.
try {
$ArchiveStream = [IO.File]::Open(
    $ZipStage,
    [IO.FileMode]::CreateNew,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
$Archive = $null
try {
    $Archive = [IO.Compression.ZipArchive]::new(
        $ArchiveStream,
        [IO.Compression.ZipArchiveMode]::Create,
        $false
    )
    foreach ($Item in @(Get-ChildItem -LiteralPath $PortableRoot -Recurse -File | Sort-Object FullName)) {
        $Relative = $Item.FullName.Substring($PortableRoot.Length + 1).Replace('\', '/')
        $EntryName = "RigPilotAgent-Portable/$Relative"
        $Entry = $Archive.CreateEntry(
            $EntryName,
            [IO.Compression.CompressionLevel]::Optimal
        )
        $Source = [IO.File]::OpenRead($Item.FullName)
        $Destination = $Entry.Open()
        try {
            $Source.CopyTo($Destination)
        }
        finally {
            $Destination.Dispose()
            $Source.Dispose()
        }
    }
}
finally {
    if ($null -ne $Archive) {
        $Archive.Dispose()
    }
    $ArchiveStream.Dispose()
}
if (-not (Test-Path -LiteralPath $ZipStage -PathType Leaf)) {
    throw "Windows 便携版 ZIP 没有生成。"
}
# Reopen the finished archive and hash every entry before publishing its
# outer SHA-256. A successful ZipArchive write alone does not prove that the
# central directory and every compressed payload can be read back intact.
$ExpectedArchiveFiles = @{}
foreach ($Relative in $ManifestFiles.Keys) {
    $EntryName = "RigPilotAgent-Portable/$Relative"
    $ExpectedArchiveFiles[$EntryName] = $ManifestFiles[$Relative]
}
$ExpectedArchiveFiles["RigPilotAgent-Portable/agent-release.json"] =
    (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$VerifiedArchiveFiles = @{}
$VerifyStream = [IO.File]::Open(
    $ZipStage,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
$VerifyArchive = $null
try {
    $VerifyArchive = [IO.Compression.ZipArchive]::new(
        $VerifyStream,
        [IO.Compression.ZipArchiveMode]::Read,
        $false
    )
    foreach ($Entry in $VerifyArchive.Entries) {
        if (-not $ExpectedArchiveFiles.ContainsKey($Entry.FullName) -or
            $VerifiedArchiveFiles.ContainsKey($Entry.FullName)) {
            throw "Windows 便携版 ZIP 回读时发现未知或重复条目：$($Entry.FullName)"
        }
        $EntryStream = $Entry.Open()
        $Hasher = [Security.Cryptography.SHA256]::Create()
        try {
            $EntryDigest = ([BitConverter]::ToString($Hasher.ComputeHash($EntryStream))).Replace('-', '').ToLowerInvariant()
        }
        finally {
            $Hasher.Dispose()
            $EntryStream.Dispose()
        }
        if ($EntryDigest -ne $ExpectedArchiveFiles[$Entry.FullName]) {
            throw "Windows 便携版 ZIP 回读 SHA-256 不匹配：$($Entry.FullName)"
        }
        $VerifiedArchiveFiles[$Entry.FullName] = $true
    }
}
finally {
    if ($null -ne $VerifyArchive) {
        $VerifyArchive.Dispose()
    }
    $VerifyStream.Dispose()
}
if ($VerifiedArchiveFiles.Count -ne $ExpectedArchiveFiles.Count) {
    throw "Windows 便携版 ZIP 回读文件清单不完整。"
}
$ZipHash = (Get-FileHash -LiteralPath $ZipStage -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(
    $ZipHashStage,
    "$ZipHash  $ArchiveName`n",
    (New-Object System.Text.ASCIIEncoding)
)
# Native file moves refuse existing destinations, including directories.
# Preserve public files on a partial publication; only private stages are ours
# to clean up after a collision or sidecar failure.
[IO.File]::Move($ZipStage, $ZipPath)
[IO.File]::Move($ZipHashStage, $ZipHashPath)
$PortableBytes = (Get-ChildItem -LiteralPath $PortableRoot -Recurse -File | Measure-Object Length -Sum).Sum
[PSCustomObject]@{
    PortableZip = $ZipPath
    PortableSha256 = $ZipHash
    PortableSha256File = $ZipHashPath
    FrozenAgent = Join-Path $PortableRoot "RigPilotAgent.exe"
    PortableBytes = $PortableBytes
    InstallsService = $false
    CreatesScheduledTask = $false
    BootstrapOnly = ($ReleaseVersion -eq "0.5.6")
} | Format-List
}
finally {
    Remove-Item -LiteralPath $ZipStage, $ZipHashStage -Force -ErrorAction SilentlyContinue
}
}
finally {
    if (Test-Path -LiteralPath $WorkRoot) {
        try {
            Remove-Item -LiteralPath $WorkRoot -Recurse -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Windows 便携版临时构建目录清理失败：$WorkRoot"
        }
    }
}
