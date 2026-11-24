param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python -PathType Leaf)) {
    throw "Existing .venv has no Windows Python interpreter. Preserve it and recreate it with Windows Python; see README.md."
}

& $Python -m pip install -r "server\requirements.txt"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $env:RIGPILOT_ADMIN_PASSWORD -and -not (Test-Path "data\rigpilot.db")) {
    Write-Host "RIGPILOT_ADMIN_PASSWORD 未设置，服务器会在首次启动日志中打印一次性密码。" -ForegroundColor Yellow
}

& $Python -m uvicorn server.main:app --host $HostAddress --port $Port
exit $LASTEXITCODE
