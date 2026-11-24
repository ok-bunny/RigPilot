[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DataPath
)

$ErrorActionPreference = "Stop"
$Expected = [IO.Path]::GetFullPath((Join-Path $env:ProgramData "RigPilot"))
$Requested = [IO.Path]::GetFullPath($DataPath)
if (-not [string]::Equals(
    $Expected.TrimEnd('\'),
    $Requested.TrimEnd('\'),
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "拒绝删除非固定 RigPilot ProgramData 目录：$Requested"
}
if (-not (Test-Path -LiteralPath $Expected)) {
    return
}
$Item = Get-Item -LiteralPath $Expected -Force
if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw "拒绝递归删除符号链接或 junction。"
}
$NestedReparsePoint = Get-ChildItem -LiteralPath $Expected -Force -Recurse |
    Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint } |
    Select-Object -First 1
if ($NestedReparsePoint) {
    throw "拒绝清理包含符号链接或 junction 的 ProgramData：$($NestedReparsePoint.FullName)"
}
Remove-Item -LiteralPath $Expected -Recurse -Force
