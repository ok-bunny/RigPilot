"""Exercise the shared Windows enrollment functions without launching an Agent."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "deploy/windows/portable/portable-agent.ps1"

# Parse and load only the enrollment functions. The production top-level entry,
# elevation, task installation and process-launching code are never evaluated.
HARNESS = r'''
param([string]$Source, [string]$Workspace, [string]$Scenario, [int]$ScheduledMode)
$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw "Launcher parse failed" }
foreach ($Name in @("Test-PortablePending", "Test-PortableEnrollmentPending",
    "Test-PortableReplacementPending", "Assert-PortableFirstContactTargetAvailable",
    "Invoke-PortableEnrollmentAttempt", "Invoke-Enrollment", "Invoke-Inheritance",
    "Invoke-EnrollmentRecovery", "Invoke-InheritanceRecovery", "Initialize-PortableEnrollment",
    "Test-ControllerUrl", "Write-PortableConfig")) {
    $Nodes = @($Ast.FindAll({ param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name
    }, $true))
    if ($Nodes.Count -ne 1) { throw "Enrollment function is missing or ambiguous" }
    . ([ScriptBlock]::Create($Nodes[0].Extent.Text))
}
$PortableRoot = $Workspace
$ProtectedBase = Join-Path $Workspace "programdata"
$DataDir = Join-Path $Workspace "data"
$ConfigPath = Join-Path $DataDir "config.json"
$StatePath = Join-Path $DataDir "state.json"
$PendingPath = Join-Path $DataDir ".state.json.enrollment-pending"
$ReplacementPendingPath = Join-Path $DataDir ".state.json.replacement-pending"
$AgentExe = "Invoke-FakeAgent"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$OriginalToken = "rpe_" + ("T" * 43)
$DifferentToken = "rpe_" + ("N" * 43)
$OriginalReplacement = "rpr_" + ("R" * 43)
$DifferentReplacement = "rpr_" + ("D" * 43)
$script:AgentCalls = @()
$script:AclLockCalls = 0
$script:AclPrivateChecks = 0
$script:AclHelperCodePresence = @()
$script:Prompts = @()
$Errors = @()
[void][IO.Directory]::CreateDirectory($DataDir)
if ($Scenario -notin @("inherit-explicit-unattended", "inherit-explicit-unattended-missing-url",
    "inherit-explicit-unattended-bad-url", "fresh-explicit-unattended")) {
    [IO.File]::WriteAllText($ConfigPath, '{}')
}
$InitialPending = $Scenario -in @("recover", "retry-original", "retry-different", "leave-blank",
    "corrupt", "directory", "fail-scheduled")
if ($InitialPending) {
    if ($Scenario -eq "directory") {
        [void][IO.Directory]::CreateDirectory($PendingPath)
    }
    else {
        # Deliberately opaque/invalid JSON: the launcher must never parse it.
        [IO.File]::WriteAllText($PendingPath, "same-test-pending-proof")
    }
}
if ($Scenario -in @("inherit-pending-retry", "inherit-pending-wrong",
    "inherit-conflict", "inherit-pending-different-explicit")) {
    [IO.File]::WriteAllText($ReplacementPendingPath, "same-test-replacement-proof")
}
if ($Scenario -eq "inherit-conflict") {
    [IO.File]::WriteAllText($PendingPath, "same-test-pending-proof")
}
if ($Scenario -eq "inherit-on-occupied") {
    [void][IO.Directory]::CreateDirectory((Join-Path $ProtectedBase "RigPilotAgentPortable"))
}
if ($Scenario -eq "existing-state") {
    [IO.File]::WriteAllText($StatePath, '{}')
}

function Assert-NotReparsePoint([string]$Path, [string]$Label) { }
function Get-TrustedProgramDataRoot { return $ProtectedBase }
function Protect-PortableFirstContactData {
    # This isolated helper boundary observes what the real icacls child would
    # inherit, without invoking icacls or changing any Windows ACL.
    $CodePresent = (-not [string]::IsNullOrEmpty(
        [Environment]::GetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", "Process")
    )) -or (-not [string]::IsNullOrEmpty(
        [Environment]::GetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", "Process")
    ))
    $script:AclHelperCodePresence += [bool]$CodePresent
    if ($CodePresent) { throw "A first-contact code reached the ACL helper environment." }
    $script:AclLockCalls += 1
}
function Assert-PortableFirstContactDataPrivate { $script:AclPrivateChecks += 1 }
function Update-PortableConfig { }
function Read-Host([string]$Prompt) {
    if ($Scenario -like "inherit-*") { return "2" }
    return "1"
}
function Read-PlainSecret([string]$Prompt) {
    $script:Prompts += $Prompt
    if ($Scenario -in @("leave-blank", "corrupt")) { return "" }
    if ($Scenario -eq "retry-different") { return $DifferentToken }
    if ($Scenario -eq "new-rejected" -and $script:Prompts.Count -eq 1) { return $DifferentToken }
    if ($Scenario -eq "inherit-pending-wrong") { return $DifferentReplacement }
    if ($Scenario -eq "inherit-bad-format") { return "not-a-valid-code" }
    if ($Scenario -like "inherit-*") { return $OriginalReplacement }
    return $OriginalToken
}
function Finish-FakeEnrollment {
    [IO.File]::WriteAllText($StatePath, '{}')
    if ([IO.File]::Exists($PendingPath)) { [IO.File]::Delete($PendingPath) }
    if ([IO.File]::Exists($ReplacementPendingPath)) { [IO.File]::Delete($ReplacementPendingPath) }
    $global:LASTEXITCODE = 0
}
function Read-FakePendingPayload([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return $null }
    $Attributes = [IO.File]::GetAttributes($Path)
    if (($Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        ($Attributes -band [IO.FileAttributes]::Directory) -ne 0) { return $null }
    return [IO.File]::ReadAllText($Path)
}
function Invoke-FakeAgent {
    if ($script:AclLockCalls -lt 1 -or $script:AclPrivateChecks -lt 1) {
        throw "The Agent was contacted before source data ACL protection."
    }
    if ($args.Count -ne 3 -or $args[0] -ne "--config" -or $args[1] -ne $ConfigPath -or $args[2] -ne "--once") {
        throw "Unexpected Agent arguments"
    }
    $Token = [Environment]::GetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", "Process")
    $Replacement = [Environment]::GetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", "Process")
    if (-not [string]::IsNullOrEmpty($Token) -and -not [string]::IsNullOrEmpty($Replacement)) {
        throw "Both first-contact codes reached the Agent"
    }
    if (-not [string]::IsNullOrEmpty($Replacement)) {
        $Kind = if ($Replacement -ceq $OriginalReplacement) { "inherit-original" } else { "inherit-different" }
        $HadPending = Test-PortableReplacementPending
        $Payload = Read-FakePendingPayload $ReplacementPendingPath
        $script:AgentCalls += [pscustomobject]@{ token_kind=$Kind; had_pending=$HadPending; pending_payload=$Payload }
        $global:LASTEXITCODE = 1
        if ($HadPending) {
            if ($Kind -eq "inherit-original" -and $Scenario -in @(
                "inherit-pending-retry", "inherit-lost"
            )) { Finish-FakeEnrollment }
            return
        }
        [IO.File]::WriteAllText($ReplacementPendingPath, "same-test-replacement-proof")
        if ($Scenario -eq "inherit-lost") { return }
        Finish-FakeEnrollment
        return
    }
    $Kind = if ([string]::IsNullOrEmpty($Token)) { "none" } elseif ($Token -ceq $OriginalToken) { "original" } else { "different" }
    $HadPending = Test-PortableEnrollmentPending
    $Payload = Read-FakePendingPayload $PendingPath
    $script:AgentCalls += [pscustomobject]@{ token_kind=$Kind; had_pending=$HadPending; pending_payload=$Payload }
    $global:LASTEXITCODE = 1
    if ($HadPending) {
        if ($Scenario -in @("fail-scheduled", "leave-blank", "corrupt", "directory")) { return }
        if ($Scenario -in @("retry-original", "retry-different") -and $Kind -ne "original") { return }
        if ($Scenario -notin @("recover", "retry-original", "new-lost")) { return }
        Finish-FakeEnrollment
        return
    }
    if ($Scenario -notin @("fresh", "fresh-explicit", "fresh-explicit-scheduled",
        "fresh-explicit-unattended", "new-lost", "new-rejected") -or $Kind -eq "none") {
        throw "The launcher tried to replace a pending request or enroll without a token"
    }
    [IO.File]::WriteAllText($PendingPath, "same-test-pending-proof")
    if ($script:AgentCalls.Count -eq 1) {
        if ($Scenario -eq "new-lost") { return }
        if ($Scenario -eq "new-rejected") {
            # Only the simulated runtime handles exact not-created cleanup.
            [IO.File]::Delete($PendingPath)
            $global:LASTEXITCODE = 10
            return
        }
    }
    Finish-FakeEnrollment
}

# Every scenario without an explicit mode starts with a clean process environment.
[Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $null, "Process")
[Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $null, "Process")
[Environment]::SetEnvironmentVariable("RIGPILOT_CONTROLLER_URL", $null, "Process")
[Environment]::SetEnvironmentVariable("RIGPILOT_DEVICE_NAME", $null, "Process")
if ($Scenario -in @("inherit-explicit", "inherit-explicit-scheduled")) {
    [Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $OriginalReplacement, "Process")
}
if ($Scenario -eq "inherit-pending-different-explicit") {
    [Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $OriginalToken, "Process")
}
if ($Scenario -in @("inherit-explicit-unattended", "inherit-explicit-unattended-missing-url",
    "inherit-explicit-unattended-bad-url")) {
    [Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $OriginalReplacement, "Process")
}
if ($Scenario -in @("fresh-explicit", "fresh-explicit-scheduled", "fresh-explicit-unattended")) {
    [Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $OriginalToken, "Process")
}
if ($Scenario -in @("inherit-explicit-conflict", "existing-state")) {
    [Environment]::SetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", $OriginalToken, "Process")
    [Environment]::SetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", $OriginalReplacement, "Process")
}
if ($Scenario -in @("inherit-explicit-unattended", "fresh-explicit-unattended")) {
    [Environment]::SetEnvironmentVariable("RIGPILOT_CONTROLLER_URL", "https://controller.example.test", "Process")
    [Environment]::SetEnvironmentVariable("RIGPILOT_DEVICE_NAME", "replacement-windows", "Process")
}
if ($Scenario -eq "inherit-explicit-unattended-missing-url") {
    [Environment]::SetEnvironmentVariable("RIGPILOT_DEVICE_NAME", "replacement-windows", "Process")
}
if ($Scenario -eq "inherit-explicit-unattended-bad-url") {
    [Environment]::SetEnvironmentVariable("RIGPILOT_CONTROLLER_URL", "http://not-local.example.test", "Process")
    [Environment]::SetEnvironmentVariable("RIGPILOT_DEVICE_NAME", "replacement-windows", "Process")
}
$Rounds = if ($Scenario -in @("new-lost", "new-rejected", "inherit-lost")) { 2 } else { 1 }
for ($Round = 0; $Round -lt $Rounds; $Round++) {
    try { Initialize-PortableEnrollment ([bool]$ScheduledMode) }
    catch { $Errors += $_.Exception.Message }
}
$PayloadAfter = Read-FakePendingPayload $PendingPath
$ReplacementPayloadAfter = Read-FakePendingPayload $ReplacementPendingPath
[pscustomobject]@{
    calls=@($script:AgentCalls); prompts=@($script:Prompts); errors=@($Errors)
    acl_lock_calls=$script:AclLockCalls; acl_private_checks=$script:AclPrivateChecks
    acl_helper_code_presence=@($script:AclHelperCodePresence)
    has_state=(Test-Path -LiteralPath $StatePath -PathType Leaf)
    has_config=(Test-Path -LiteralPath $ConfigPath -PathType Leaf)
    has_pending=(Test-PortableEnrollmentPending); pending_payload=$PayloadAfter
    has_replacement_pending=(Test-PortableReplacementPending)
    replacement_pending_payload=$ReplacementPayloadAfter
    token_cleared=([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("RIGPILOT_ENROLLMENT_TOKEN", "Process")))
    replacement_cleared=([string]::IsNullOrEmpty([Environment]::GetEnvironmentVariable("RIGPILOT_REPLACEMENT_TOKEN", "Process")))
} | ConvertTo-Json -Depth 8 -Compress
'''


@pytest.fixture
def portable_enrollment_runner(tmp_path):
    """Share the isolated AST harness; enrollment expectations stay per mode."""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    harness = tmp_path / "enrollment-harness.ps1"
    harness.write_text(HARNESS, encoding="utf-8")
    workspace = tmp_path / "portable path with spaces"
    workspace.mkdir()

    def run(scenario, scheduled):
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
             "-File", str(harness), "-Source", str(LAUNCHER), "-Workspace", str(workspace),
             "-Scenario", scenario, "-ScheduledMode", str(int(scheduled))],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    return workspace, run


@pytest.mark.skipif(os.name != "nt", reason="Exercises the real Windows PowerShell 5.1 functions")
@pytest.mark.parametrize(
    "scenario,scheduled,token_kinds,prompt_count,error_count,has_state,has_pending",
    [
        ("recover", False, ["none"], 0, 0, True, False),
        ("recover", True, ["none"], 0, 0, True, False),
        ("fail-scheduled", True, ["none"], 0, 1, False, True),
        ("leave-blank", False, ["none"], 1, 1, False, True),
        ("retry-original", False, ["none", "original"], 1, 0, True, False),
        ("retry-different", False, ["none", "different"], 1, 1, False, True),
        ("corrupt", False, ["none"], 1, 1, False, True),
        ("directory", True, ["none"], 0, 1, False, True),
        ("dangling-link", True, ["none"], 0, 1, False, True),
        ("missing-pending", True, [], 0, 1, False, False),
        ("new-lost", False, ["original", "none"], 1, 1, True, False),
        ("new-rejected", False, ["different", "original"], 2, 1, True, False),
        ("fresh", False, ["original"], 1, 0, True, False),
    ],
)
def test_shared_portable_enrollment_routes_recovery_before_new_token(
    portable_enrollment_runner, scenario, scheduled, token_kinds, prompt_count, error_count, has_state, has_pending,
):
    workspace, run = portable_enrollment_runner
    if scenario == "dangling-link":
        data = workspace / "data"
        data.mkdir()
        try:
            os.symlink(data / "missing-target", data / ".state.json.enrollment-pending")
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                pytest.skip("Creating this dangling-link fixture requires Windows symlink permission")
            raise
    result = run(scenario, scheduled)
    assert [call["token_kind"] for call in result["calls"]] == token_kinds
    assert len(result["prompts"]) == prompt_count
    assert len(result["errors"]) == error_count
    assert result["has_state"] is has_state
    assert result["has_pending"] is has_pending
    assert not any(result["acl_helper_code_presence"])
    if token_kinds:
        assert result["token_cleared"] and result["replacement_cleared"]
        assert result["acl_lock_calls"] >= 1 and result["acl_private_checks"] >= 1
    if scenario in {"retry-original", "retry-different"}:
        assert all(call["had_pending"] for call in result["calls"])
        assert {call["pending_payload"] for call in result["calls"]} == {"same-test-pending-proof"}
        assert "Original enrollment token" in result["prompts"][0]
    if has_pending and scenario not in {"directory", "dangling-link"}:
        assert result["pending_payload"] == "same-test-pending-proof"
    if scenario == "new-lost":
        assert result["calls"][0]["had_pending"] is False
        assert result["calls"][1]["had_pending"] is True
    if scenario == "new-rejected":
        assert all(call["had_pending"] is False for call in result["calls"])


@pytest.mark.skipif(os.name != "nt", reason="Exercises the real Windows PowerShell 5.1 functions")
@pytest.mark.parametrize(
    "scenario,scheduled,token_kinds,prompt_count,error_count,has_state,has_pending",
    [
        ("inherit-fresh", False, ["inherit-original"], 1, 0, True, False),
        ("inherit-lost", False, ["inherit-original", "inherit-original"], 2, 1, True, False),
        ("inherit-pending-retry", False, ["inherit-original"], 1, 0, True, False),
        ("inherit-pending-wrong", False, ["inherit-different"], 1, 1, False, True),
        ("inherit-bad-format", False, [], 1, 1, False, False),
        ("inherit-explicit", False, ["inherit-original"], 0, 0, True, False),
        ("inherit-explicit-scheduled", True, ["inherit-original"], 0, 0, True, False),
        ("fresh-explicit", False, ["original"], 0, 0, True, False),
        ("fresh-explicit-scheduled", True, ["original"], 0, 0, True, False),
        ("inherit-explicit-unattended", True, ["inherit-original"], 0, 0, True, False),
        ("fresh-explicit-unattended", True, ["original"], 0, 0, True, False),
        ("inherit-explicit-unattended-missing-url", True, [], 0, 1, False, False),
        ("inherit-explicit-unattended-bad-url", True, [], 0, 1, False, False),
        ("inherit-conflict", False, [], 0, 1, False, True),
        ("inherit-on-occupied", False, [], 0, 1, False, False),
        ("inherit-pending-different-explicit", False, [], 0, 1, False, True),
        ("inherit-explicit-conflict", False, [], 0, 1, False, False),
        ("existing-state", False, [], 0, 0, True, False),
    ],
)
def test_portable_inheritance_mode_and_recovery_preserve_identity(
    portable_enrollment_runner, scenario, scheduled, token_kinds, prompt_count, error_count, has_state, has_pending,
):
    workspace, run = portable_enrollment_runner
    result = run(scenario, scheduled)
    assert [call["token_kind"] for call in result["calls"]] == token_kinds
    assert len(result["prompts"]) == prompt_count
    assert len(result["errors"]) == error_count
    assert result["has_state"] is has_state
    assert result["has_replacement_pending"] is has_pending
    assert result["token_cleared"] and result["replacement_cleared"]
    assert not any(result["acl_helper_code_presence"])
    if scenario in {"inherit-explicit-unattended", "fresh-explicit-unattended"}:
        config = json.loads((workspace / "data/config.json").read_text(encoding="utf-8"))
        assert config["server_url"] == "https://controller.example.test"
        assert config["name"] == "replacement-windows"
        assert "enrollment_token" not in config and "replacement_token" not in config
    if scenario in {"inherit-explicit-unattended-missing-url", "inherit-explicit-unattended-bad-url"}:
        assert result["has_config"] is False
    if token_kinds:
        assert result["acl_lock_calls"] >= 1 and result["acl_private_checks"] >= 1
    if scenario in {"inherit-pending-retry", "inherit-pending-wrong", "inherit-lost"}:
        assert result["calls"][-1]["had_pending"] is True
        assert result["calls"][-1]["pending_payload"] == "same-test-replacement-proof"
    if scenario == "inherit-pending-wrong":
        assert result["replacement_pending_payload"] == "same-test-replacement-proof"
    if scenario in {"inherit-conflict", "inherit-on-occupied", "inherit-pending-different-explicit", "inherit-explicit-conflict"}:
        assert result["calls"] == [] and result["prompts"] == []
    if scenario == "inherit-explicit-conflict":
        assert result["errors"] == ["Normal enrollment and device inheritance codes cannot be supplied together."]
    if scenario == "existing-state":
        assert result["acl_lock_calls"] == 0


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows NTFS ACLs")
def test_same_admin_first_contact_source_data_remains_private_after_migration_copy(tmp_path, request):
    import ctypes

    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("The NTFS owner/ACL gate requires an elevated Windows test process")
    owned_parent = tempfile.TemporaryDirectory(prefix="RigPilotSourceAcl-", dir=Path(tmp_path.anchor))
    request.addfinalizer(owned_parent.cleanup)
    harness = tmp_path / "source-acl-harness.ps1"
    harness.write_text(
        r'''
param([string]$Source, [string]$Workspace)
$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw "Launcher parse failed" }
foreach ($Name in @(
    "Assert-NotReparsePoint", "New-PortableBoundaryAcl", "Repair-PortableAclChildren",
    "Get-PortablePlainPaths",
    "Assert-PortablePrivateAcl", "Assert-PortableProtectedAcl",
    "Assert-PortableSourceParentChain", "Protect-PortableFirstContactData",
    "Assert-PortableFirstContactDataPrivate"
)) {
    $Nodes = @($Ast.FindAll({ param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name
    }, $true))
    if ($Nodes.Count -ne 1) { throw "ACL function missing or ambiguous: $Name" }
    . ([ScriptBlock]::Create($Nodes[0].Extent.Text))
}
# Change only this newly created test-owned leaf; keep the real volume root.
$FixtureParent = [IO.Directory]::GetParent($Workspace).FullName
$CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$ParentAcl = [Security.AccessControl.DirectorySecurity]::new()
$ParentAcl.SetAccessRuleProtection($true, $false)
$ParentAcl.SetOwner($CurrentSid)
$TrustedSids = @("S-1-5-18", "S-1-5-32-544", $CurrentSid.Value) | Select-Object -Unique
foreach ($SidValue in $TrustedSids) {
    $Sid = [Security.Principal.SecurityIdentifier]::new($SidValue)
    $ParentAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $Sid, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    ))
}
Set-Acl -LiteralPath $FixtureParent -AclObject $ParentAcl
$PortableRoot = $Workspace
$DataDir = Join-Path $Workspace "data"
$Nested = Join-Path $DataDir "logs"
[void][IO.Directory]::CreateDirectory($Nested)
[IO.File]::WriteAllText((Join-Path $DataDir "config.json"), "{}")
[IO.File]::WriteAllText((Join-Path $DataDir ".state.json.replacement-pending"), "private-pending")
[IO.File]::WriteAllText((Join-Path $Nested "agent.jsonl"), "private-log")
Protect-PortableFirstContactData
$StatePath = Join-Path $DataDir "state.json"
[IO.File]::WriteAllText($StatePath, "private-inherited-agent-secret")
Protect-PortableFirstContactData
Assert-PortableFirstContactDataPrivate
$Copied = Join-Path $Workspace "migration-copy"
Copy-Item -LiteralPath $DataDir -Destination $Copied -Recurse
Assert-PortableFirstContactDataPrivate
$StateAcl = Get-Acl -LiteralPath $StatePath
$AllowSids = @($StateAcl.Access | Where-Object {
    $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow
} | ForEach-Object {
    $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
} | Sort-Object -Unique)
[pscustomobject]@{
    source_state_exists=(Test-Path -LiteralPath $StatePath -PathType Leaf)
    source_state_allow_sids=$AllowSids
    copied_state_exists=(Test-Path -LiteralPath (Join-Path $Copied "state.json") -PathType Leaf)
} | ConvertTo-Json -Depth 5 -Compress
''',
        encoding="utf-8",
    )
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
         "-File", str(harness), "-Source", str(LAUNCHER), "-Workspace", str(Path(owned_parent.name) / "portable-source")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["source_state_exists"] and result["copied_state_exists"]
    assert set(result["source_state_allow_sids"]) == {"S-1-5-18", "S-1-5-32-544"}


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows NTFS ACLs")
def test_private_profile_source_accepts_system_volume_root_but_rejects_broad_parent(tmp_path):
    """A normal same-user extraction is safe; an untrusted delete-child ACE is not."""
    try:
        owned_parent = tempfile.TemporaryDirectory(prefix="RigPilotParentAcl-", dir=Path(tmp_path.anchor))
    except PermissionError as error:
        if getattr(error, "winerror", None) in {5, 1314}:
            pytest.skip("Creating an owned NTFS volume-root ACL fixture requires this Windows capability")
        raise
    with owned_parent as temporary:
        source_root = Path(temporary) / "RigPilotAgent-Portable"
        harness = tmp_path / "parent-owner-harness.ps1"
        harness.write_text(
            r"""
param([string]$Source, [string]$PortableSource)
$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw "Launcher parse failed" }
$Nodes = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Assert-PortableSourceParentChain"
}, $true))
if ($Nodes.Count -ne 1) { throw "Parent gate missing or ambiguous" }
. ([ScriptBlock]::Create($Nodes[0].Extent.Text))
$PortableRoot = $PortableSource
$FixtureRoot = [IO.Directory]::GetParent($PortableRoot).FullName
New-Item -ItemType Directory -Path $PortableRoot -Force | Out-Null
try {
$DriveRoot = [IO.Path]::GetPathRoot($PortableRoot)
$Drive = [IO.DriveInfo]::new($DriveRoot)
if ($Drive.DriveType -ne [IO.DriveType]::Fixed -or $Drive.DriveFormat -cne "NTFS") {
    throw "This native test needs a fixed NTFS user profile"
}
# Change only this newly created test-owned leaf; keep the real volume root.
$FixtureParent = $FixtureRoot
$CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$ParentAcl = [Security.AccessControl.DirectorySecurity]::new()
$ParentAcl.SetAccessRuleProtection($true, $false)
$ParentAcl.SetOwner($CurrentSid)
$TrustedSids = @("S-1-5-18", "S-1-5-32-544", $CurrentSid.Value) | Select-Object -Unique
foreach ($SidValue in $TrustedSids) {
    $Sid = [Security.Principal.SecurityIdentifier]::new($SidValue)
    $ParentAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $Sid, [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    ))
}
Set-Acl -LiteralPath $FixtureParent -AclObject $ParentAcl
$RootAcl = Get-Acl -LiteralPath $DriveRoot
try { $RootOwner = (New-Object Security.Principal.NTAccount($RootAcl.Owner)).Translate(
        [Security.Principal.SecurityIdentifier]).Value }
catch { $RootOwner = [string]$RootAcl.Owner }
try { Assert-PortableSourceParentChain }
catch { throw "Unexpected initial rejection: $($_.Exception.Message)" }
$Parent = [IO.Directory]::GetParent($PortableRoot).FullName
$ParentAcl = Get-Acl -LiteralPath $Parent
$AuthenticatedUsers = [Security.Principal.SecurityIdentifier]::new("S-1-5-11")
$ParentAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $AuthenticatedUsers,
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles,
    [Security.AccessControl.AccessControlType]::Allow
))
Set-Acl -LiteralPath $Parent -AclObject $ParentAcl
$Rejected = $false
try { Assert-PortableSourceParentChain }
catch {
    if ($_.Exception.Message -like "*broadly writable*") { $Rejected = $true }
    else { throw "Unexpected post-ACL rejection: $($_.Exception.Message)" }
}
if (-not $Rejected) { throw "Authenticated Users could delete the portable source" }
[pscustomobject]@{
    private_source_passed=$true
    untrusted_delete_child_rejected=$Rejected
    volume_root_owner_sid=$RootOwner
    volume_root_acl_protected=[bool]$RootAcl.AreAccessRulesProtected
} | ConvertTo-Json -Compress
} finally { Remove-Item -LiteralPath $FixtureRoot -Recurse -Force }
""",
            encoding="utf-8",
        )
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
             "-File", str(harness), "-Source", str(LAUNCHER), "-PortableSource", str(source_root)],
            capture_output=True, text=True, timeout=45, check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result = json.loads(completed.stdout)
        assert result["private_source_passed"] is True
        assert result["untrusted_delete_child_rejected"] is True
        if result["volume_root_owner_sid"] == (
            "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
        ):
            assert result["volume_root_acl_protected"] is True


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows NTFS ACLs")
def test_first_contact_rejects_service_sid_delete_child_on_parent(tmp_path):
    import ctypes

    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("Changing an NTFS parent ACL requires an elevated Windows test process")
    harness = tmp_path / "service-sid-parent-harness.ps1"
    harness.write_text(
        r'''
param([string]$Source, [string]$Workspace)
$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw "Launcher parse failed" }
$Nodes = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Assert-PortableSourceParentChain"
}, $true))
if ($Nodes.Count -ne 1) { throw "Parent ACL gate missing or ambiguous" }
. ([ScriptBlock]::Create($Nodes[0].Extent.Text))
$Parent = Join-Path $Workspace "broad-parent"
$PortableRoot = Join-Path $Parent "source"
[void][IO.Directory]::CreateDirectory($PortableRoot)
$Acl = Get-Acl -LiteralPath $Parent
$ServiceSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-80-1-2-3-4-5")
$Acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $ServiceSid,
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles,
    [Security.AccessControl.AccessControlType]::Allow
))
Set-Acl -LiteralPath $Parent -AclObject $Acl
$Rejected = $false
try {
    Assert-PortableSourceParentChain
}
catch {
    if ($_.Exception.Message -like "*broadly writable*") { $Rejected = $true }
    else { throw }
}
if (-not $Rejected) { throw "An untrusted service SID could delete the source directory" }
[pscustomobject]@{service_sid_rejected=$Rejected} | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
         "-File", str(harness), "-Source", str(LAUNCHER), "-Workspace", str(tmp_path)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["service_sid_rejected"] is True


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows accounts and NTFS ACLs")
def test_split_account_source_parent_is_refused_before_claim(tmp_path):
    import ctypes

    if not ctypes.windll.shell32.IsUserAnAdmin():
        pytest.skip("Requires the elevated installation account")
    other_parent = os.environ.get("RIGPILOT_TEST_SPLIT_ACCOUNT_PARENT")
    if not other_parent:
        pytest.skip("Set RIGPILOT_TEST_SPLIT_ACCOUNT_PARENT to an existing directory owned by a different local account")
    if not Path(other_parent).is_dir():
        pytest.skip("The split-account parent fixture is unavailable")
    harness = tmp_path / "split-account-parent-harness.ps1"
    harness.write_text(
        r'''
param([string]$Source, [string]$OtherParent)
$ErrorActionPreference = "Stop"
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw "Launcher parse failed" }
$Nodes = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Assert-PortableSourceParentChain"
}, $true))
if ($Nodes.Count -ne 1) { throw "Parent ACL gate missing or ambiguous" }
. ([ScriptBlock]::Create($Nodes[0].Extent.Text))
$PortableRoot = Join-Path $OtherParent "not-created-source"
$Rejected = $false
try {
    Assert-PortableSourceParentChain
}
catch {
    if ($_.Exception.Message -like "*untrusted owner*" -or
        $_.Exception.Message -like "*broadly writable*") {
        $Rejected = $true
    }
    else { throw }
}
if (-not $Rejected) { throw "Split-account source parent was accepted" }
[pscustomobject]@{split_account_refused=$Rejected} | ConvertTo-Json -Compress
''',
        encoding="utf-8",
    )
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned",
         "-File", str(harness), "-Source", str(LAUNCHER), "-OtherParent", other_parent],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["split_account_refused"] is True
