[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$DataPath,

    [switch]$ResetUntrusted
)

$ErrorActionPreference = "Stop"
$Expected = [IO.Path]::GetFullPath((Join-Path $env:ProgramData "RigPilot"))
$Requested = [IO.Path]::GetFullPath($DataPath)
if (-not [string]::Equals(
    $Expected.TrimEnd('\'),
    $Requested.TrimEnd('\'),
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "拒绝修改非固定 ProgramData 目录的 ACL：$Requested"
}

$SystemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$AdministratorsSid = [Security.Principal.SecurityIdentifier]::new(
    "S-1-5-32-544"
)
$AllowedSids = @($SystemSid.Value, $AdministratorsSid.Value)

function Set-ExactRigPilotAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [bool]$IsDirectory
    )

    $Acl = Get-Acl -LiteralPath $Path
    $Acl.SetAccessRuleProtection($true, $false)
    foreach ($Rule in @($Acl.Access)) {
        [void]$Acl.RemoveAccessRuleSpecific($Rule)
    }
    if ($IsDirectory) {
        $Inheritance =
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        $SystemRule = [Security.AccessControl.FileSystemAccessRule]::new(
            $SystemSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $Inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $AdministratorsRule = [Security.AccessControl.FileSystemAccessRule]::new(
            $AdministratorsSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $Inheritance,
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
    }
    else {
        $SystemRule = [Security.AccessControl.FileSystemAccessRule]::new(
            $SystemSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $AdministratorsRule = [Security.AccessControl.FileSystemAccessRule]::new(
            $AdministratorsSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
    }
    [void]$Acl.AddAccessRule($SystemRule)
    [void]$Acl.AddAccessRule($AdministratorsRule)
    $Acl.SetOwner($AdministratorsSid)
    Set-Acl -LiteralPath $Path -AclObject $Acl

    $Verified = Get-Acl -LiteralPath $Path
    if (-not $Verified.AreAccessRulesProtected) {
        throw "ACL 仍在继承父目录权限：$Path"
    }
    foreach ($Rule in @($Verified.Access)) {
        try {
            $Sid = $Rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            $Sid = $Rule.IdentityReference.Value
        }
        if ($Sid -notin $AllowedSids -or
            $Rule.AccessControlType -ne
                [Security.AccessControl.AccessControlType]::Allow -or
            $Rule.FileSystemRights -ne
                [Security.AccessControl.FileSystemRights]::FullControl) {
            throw "ACL 含有未授权主体或非完全控制规则：$Path ($Sid)"
        }
    }
    if (@($Verified.Access).Count -ne 2) {
        throw "ACL 规则数量不符合预期：$Path"
    }
    try {
        $OwnerSid = [Security.Principal.NTAccount]::new(
            $Verified.Owner
        ).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        $OwnerSid = $Verified.Owner
    }
    if ($OwnerSid -notin $AllowedSids) {
        throw "ACL 所有者不是 SYSTEM 或 Administrators：$Path ($OwnerSid)"
    }
}

function New-SecureDataRoot {
    $Parent = Split-Path -Parent $Expected
    $Staging = Join-Path $Parent (
        "RigPilot.new-" + [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $Staging | Out-Null
    try {
        Set-ExactRigPilotAcl -Path $Staging -IsDirectory $true
        # Directory.Move is an atomic same-volume rename and refuses to merge
        # into a path an unprivileged process raced to create.
        [IO.Directory]::Move($Staging, $Expected)
    }
    catch {
        if (Test-Path -LiteralPath $Staging) {
            Remove-Item -LiteralPath $Staging -Force
        }
        throw
    }
    return Get-Item -LiteralPath $Expected -Force
}

function Get-SafeTreeItems {
    param([Parameter(Mandatory = $true)][IO.DirectoryInfo]$Root)

    $Result = [Collections.Generic.List[IO.FileSystemInfo]]::new()
    $Pending = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $Pending.Push($Root)
    while ($Pending.Count -gt 0) {
        $Directory = $Pending.Pop()
        foreach ($Child in $Directory.EnumerateFileSystemInfos()) {
            if ($Child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "ProgramData\RigPilot 不能包含符号链接或 junction：$($Child.FullName)"
            }
            $Result.Add($Child)
            if ($Child -is [IO.DirectoryInfo]) {
                $Pending.Push($Child)
            }
        }
    }
    return $Result
}

$QuarantinePath = $null
if (Test-Path -LiteralPath $Expected) {
    $Root = Get-Item -LiteralPath $Expected -Force
    if ($Root.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "ProgramData\RigPilot 不能是符号链接或 junction。"
    }
    if (-not $Root.PSIsContainer) {
        throw "ProgramData\RigPilot 必须是目录。"
    }

    # Close the active path to low-privilege users before examining any child.
    Set-ExactRigPilotAcl -Path $Expected -IsDirectory $true
    if ($ResetUntrusted) {
        $QuarantinePath = Join-Path (
            Split-Path -Parent $Expected
        ) ("RigPilot.untrusted-" + [Guid]::NewGuid().ToString("N"))
        Move-Item -LiteralPath $Expected -Destination $QuarantinePath
        $Root = New-SecureDataRoot
    }
}
else {
    $Root = New-SecureDataRoot
}

Set-ExactRigPilotAcl -Path $Expected -IsDirectory $true
$ExistingItems = @(Get-SafeTreeItems -Root $Root)
foreach ($Item in $ExistingItems | Sort-Object { $_.FullName.Length }) {
    Set-ExactRigPilotAcl -Path $Item.FullName -IsDirectory $Item.PSIsContainer
}

foreach ($Directory in @("logs", "packages")) {
    $Path = Join-Path $Expected $Directory
    if (Test-Path -LiteralPath $Path) {
        $Item = Get-Item -LiteralPath $Path -Force
        if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "ProgramData\RigPilot\$Directory 不能是符号链接或 junction。"
        }
        if (-not $Item.PSIsContainer) {
            throw "ProgramData\RigPilot\$Directory 必须是目录。"
        }
    }
    else {
        New-Item -ItemType Directory -Path $Path | Out-Null
        $Item = Get-Item -LiteralPath $Path -Force
    }
    Set-ExactRigPilotAcl -Path $Item.FullName -IsDirectory $true
}

# The untrusted tree is no longer reachable through the active data path. Clean
# it without recursive delete semantics: files (including hard links) are
# unlinked first, then empty directories. If anything changes into a reparse
# point concurrently, leave the quarantine in place instead of following it.
if ($QuarantinePath) {
    try {
        $QuarantineRoot = Get-Item -LiteralPath $QuarantinePath -Force
        $QuarantineItems = @(Get-SafeTreeItems -Root $QuarantineRoot)
        foreach ($Item in $QuarantineItems |
            Where-Object { -not $_.PSIsContainer }) {
            if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "隔离目录内容已变为重解析点：$($Item.FullName)"
            }
            Remove-Item -LiteralPath $Item.FullName -Force
        }
        foreach ($Item in $QuarantineItems |
            Where-Object { $_.PSIsContainer } |
            Sort-Object { $_.FullName.Length } -Descending) {
            $Current = Get-Item -LiteralPath $Item.FullName -Force
            if ($Current.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "隔离目录内容已变为重解析点：$($Current.FullName)"
            }
            Remove-Item -LiteralPath $Current.FullName -Force
        }
        Remove-Item -LiteralPath $QuarantinePath -Force
    }
    catch {
        Write-Warning "未自动删除隔离的旧 ProgramData：$QuarantinePath；$($_.Exception.Message)"
    }
}
