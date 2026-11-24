param([string]$Source, [string]$Workspace, [string]$Scenario)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$Tokens = $null
$Errors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count -gt 0) { throw "Builder script did not parse" }
$Functions = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Initialize-DotNet8BuildScope"
}, $true))
if ($Functions.Count -ne 1) { throw "SDK selection function is missing" }
. ([ScriptBlock]::Create($Functions[0].Extent.Text))
$WorkRoot = Join-Path $Workspace "owned-sdk-scope"
[void][IO.Directory]::CreateDirectory($WorkRoot)
$InputRoot = Join-Path $Workspace "original-helper"
[void][IO.Directory]::CreateDirectory($InputRoot)
$Inputs = @("RigPilot.CpuTemperature.csproj", "Program.cs", "packages.lock.json")
$InputHashes = @{}
foreach ($Name in $Inputs) {
    $InputPath = Join-Path $InputRoot $Name
    [IO.File]::Copy((Join-Path (Join-Path (Split-Path -Parent $Source) "cpu-temperature-helper") $Name), $InputPath)
    $InputHashes[$Name] = (Get-FileHash -LiteralPath $InputPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
$Before = (Get-Location).Path
$script:Calls = @()
function Invoke-FakeDotNet {
    $Command = [string]$args[0]
    $global:LASTEXITCODE = 0
    if ($Command -eq "--list-sdks") {
        if ($Scenario -eq "list-failure") { $global:LASTEXITCODE = 1; return }
        "9.0.300 [C:\sdk]"
        if ($Scenario -ne "missing-sdk8") {
            "8.0.99 [C:\sdk]"
            "8.0.422 [C:\sdk]"
            "8.0.500-preview.1 [C:\sdk]"
        }
        return
    }
    $JsonPath = Join-Path (Get-Location).Path "global.json"
    if (-not [IO.File]::Exists($JsonPath)) { throw "SDK command ran outside its owned scope" }
    $Sdk = (Get-Content -LiteralPath $JsonPath -Raw | ConvertFrom-Json).sdk
    if ($Command -eq "--version") {
        $script:Calls += [pscustomobject]@{ command=$Command; version=$Sdk.version; cwd=(Get-Location).Path }
        if ($Scenario -eq "resolver-mismatch") { "9.0.300" } else { $Sdk.version }
        return
    }
    if ($Command -notin @("restore", "publish")) { throw "Unexpected SDK command" }
    $Project = [IO.Path]::GetFullPath([string]$args[1])
    if (-not $Project.StartsWith($WorkRoot + [IO.Path]::DirectorySeparatorChar) -or
        -not [IO.File]::Exists($Project)) { throw "Project escaped its owned SDK scope" }
    $ProjectDirectory = Split-Path -Parent $Project
    $Current = $ProjectDirectory
    while (-not [IO.File]::Exists((Join-Path $Current "global.json"))) {
        $Parent = [IO.Directory]::GetParent($Current)
        if ($null -eq $Parent) { throw "Project resolver cannot find global.json" }
        $Current = $Parent.FullName
    }
    $ProjectSdk = (Get-Content -LiteralPath (Join-Path $Current "global.json") -Raw | ConvertFrom-Json).sdk
    if ($ProjectSdk.version -ne $Sdk.version) { throw "CLI and project SDK versions disagree" }
    $script:Calls += [pscustomobject]@{
        command=$Command; version=$Sdk.version; cwd=(Get-Location).Path
        project=$Project; project_scope=$Current
    }
    # Model project-relative intermediate output without invoking MSBuild.
    $Obj = Join-Path $ProjectDirectory "obj"
    [void][IO.Directory]::CreateDirectory($Obj)
    [IO.File]::WriteAllText((Join-Path $Obj ($Command + ".fake")), $Sdk.version)
}
$Failure = $null
$Selected = $null
try {
    $Selected = Initialize-DotNet8BuildScope "Invoke-FakeDotNet" $WorkRoot
    # Execute actual input-copy and restore/publish statements, substituting
    # only the input root and dotnet command. No build or source write runs.
    $Text = [IO.File]::ReadAllText($Source)
    $Start = $Text.IndexOf('$CpuTemperatureSource =')
    $End = $Text.IndexOf('$CpuTemperatureExe =')
    if ($Start -lt 0 -or $End -le $Start) { throw "Helper build scope is missing" }
    $HelperBlock = $Text.Substring($Start, $End - $Start).Replace(
        '$CpuTemperatureSource = Join-Path $PSScriptRoot "cpu-temperature-helper"',
        '$CpuTemperatureSource = $InputRoot')
    $DotNetPath = "Invoke-FakeDotNet"
    . ([ScriptBlock]::Create($HelperBlock))
}
catch { $Failure = $_.Exception.Message }
$GlobalJson = Join-Path $WorkRoot "global.json"
$Selection = if ([IO.File]::Exists($GlobalJson)) {
    Get-Content -LiteralPath $GlobalJson -Raw | ConvertFrom-Json
} else { $null }
$CopiedHashes = @{}
$SourceUnchanged = $true
foreach ($Name in $Inputs) {
    if ((Get-FileHash -LiteralPath (Join-Path $InputRoot $Name) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $InputHashes[$Name]) {
        $SourceUnchanged = $false
    }
    $Copy = Join-Path (Join-Path $WorkRoot "helper-source") $Name
    if ([IO.File]::Exists($Copy)) {
        $CopiedHashes[$Name] = (Get-FileHash -LiteralPath $Copy -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
[pscustomobject]@{
    selected=$Selected; failure=$Failure; calls=@($script:Calls); selection=$Selection
    cwd_restored=((Get-Location).Path -eq $Before)
    input_hashes=$InputHashes; copied_hashes=$CopiedHashes; source_unchanged=$SourceUnchanged
    source_has_obj=[IO.Directory]::Exists((Join-Path $InputRoot "obj"))
} | ConvertTo-Json -Depth 5 -Compress
