import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.version import __version__ as AGENT_VERSION


ROOT = Path(__file__).parents[1]
PORTABLE = ROOT / "deploy" / "windows" / "portable"


def _review_powershell():
    if os.name == "nt":
        return str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    return shutil.which("pwsh")


def _run_review_fixture(tmp_path, fixture, source, scenario):
    powershell = _review_powershell()
    if not powershell:
        pytest.skip("No installed PowerShell runtime; isolated native mock behavior is unverified")
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(ROOT / "tests/fixtures" / fixture), "-Source", str(source),
         "-Workspace", str(tmp_path), "-Scenario", scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _run_source_harness(tmp_path, source, scenario, body):
    """Evaluate selected source AST only; no production entry or OS setup runs."""
    powershell = _review_powershell()
    if not powershell:
        pytest.skip("No installed PowerShell runtime; isolated source behavior is unverified")
    harness = tmp_path / "source-harness.ps1"
    harness.write_text(r'''
param([string]$Source, [string]$Workspace, [string]$Scenario)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$Tokens = $null
$ParseErrors = $null
$Ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw "Source did not parse" }
''' + body, encoding="utf-8")
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(harness), "-Source", str(source), "-Workspace", str(tmp_path), "-Scenario", scenario],
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("scenario", ["preflight", "slot-collision", "incoming-failed", "switched-failed", "confirmed"])
def test_updater_failure_restores_only_this_activation(tmp_path, scenario):
    result = _run_source_harness(tmp_path, PORTABLE / "RigPilotAgentUpdater.ps1", scenario, r'''
$Flow = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst]
})[-1]
# Exercise the actual catch block with synthetic owned/unowned state. Production
# preflight, process waits, ProgramData resolution and Agent launch never run.
$CatchText = $Flow.CatchClauses[0].Body.Extent.Text.Replace(
    'exit 0', '$script:ExitCode = 0; return').Replace('exit 1', '$script:ExitCode = 1; return')
$script:Events = @()
$ActivationConfirmed = $Scenario -eq "confirmed"
$HandoffStarted = $Scenario -ne "preflight"
$PointerActivated = $Scenario -in @("switched-failed", "confirmed")
$NewLauncherProcess = $null
$Version = "1.2.3"
$ActivationId = "synthetic-job"
$CommandId = "synthetic-command"
$AgentId = "synthetic-agent"
$DataRoot = Join-Path $Workspace "data"
$SlotPath = Join-Path $Workspace "existing-slot"
$IncomingSlot = Join-Path $Workspace "owned-incoming"
$PointerPath = Join-Path $Workspace "pointer.json"
$PointerBackup = Join-Path $Workspace "pointer-backup.json"
$StatusPath = Join-Path $Workspace "status.json"
$StagedBundle = Join-Path $Workspace "bundle"
$OwnedSlotPath = $null
[void][IO.Directory]::CreateDirectory($SlotPath)
[IO.File]::WriteAllText((Join-Path $SlotPath "identity.txt"), "existing-installation")
[IO.File]::WriteAllText($PointerPath, "active-before")
[IO.File]::WriteAllText($PointerBackup, "active-before")
if ($Scenario -eq "incoming-failed") {
    [void][IO.Directory]::CreateDirectory($IncomingSlot)
    [IO.File]::WriteAllText((Join-Path $IncomingSlot "identity.txt"), "owned-new-release")
    $OwnedSlotPath = $IncomingSlot
}
if ($PointerActivated) {
    $SlotPath = Join-Path $Workspace "owned-slot"
    [void][IO.Directory]::CreateDirectory($SlotPath)
    [IO.File]::WriteAllText((Join-Path $SlotPath "identity.txt"), "owned-new-release")
    $OwnedSlotPath = $SlotPath
    [IO.File]::WriteAllText($PointerPath, "active-new")
}
function Stop-NewAgentIfOwned { $script:Events += "stop-new-agent" }
function Start-ActiveAgent { $script:Events += "restart-previous-agent" }
function Get-PlainJsonFile { return $null }
function Restore-FileAtomically([string]$From, [string]$To) {
    $script:Events += "restore-pointer"
    [IO.File]::Copy($From, $To, $true)
}
function Write-AtomicJson([string]$Path, [object]$Value) {
    $script:Events += "write-status"
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json))
}
. ([ScriptBlock]::Create('try { throw "Synthetic activation failure" } catch ' + $CatchText))
$Quarantine = Join-Path (Join-Path $DataRoot "agent-updates\quarantine") "$Version-$ActivationId"
[pscustomobject]@{
    exit_code=$script:ExitCode; events=@($script:Events)
    pointer=[IO.File]::ReadAllText($PointerPath)
    existing_identity=[IO.File]::ReadAllText((Join-Path (Join-Path $Workspace "existing-slot") "identity.txt"))
    owned_path_exists=($null -ne $OwnedSlotPath -and [IO.Directory]::Exists($OwnedSlotPath))
    quarantined_identity=$(if ([IO.Directory]::Exists($Quarantine)) {
        [IO.File]::ReadAllText((Join-Path $Quarantine "identity.txt"))
    } else { $null })
} | ConvertTo-Json -Compress
''')
    assert result["existing_identity"] == "existing-installation"
    assert result["exit_code"] == (0 if scenario == "confirmed" else 1)
    if scenario in {"preflight", "confirmed"}:
        assert result["events"] == [] and result["quarantined_identity"] is None
    else:
        # This synthetic catch fixture has no validated recovery intent. New
        # anchors must not turn that missing authority into a generic restart.
        expected = ["write-status"]
        if scenario == "switched-failed":
            expected = ["stop-new-agent", "restore-pointer"] + expected
        assert result["events"] == expected
    assert result["pointer"] == ("active-new" if scenario == "confirmed" else "active-before")
    if scenario in {"incoming-failed", "switched-failed"}:
        assert not result["owned_path_exists"]
        assert result["quarantined_identity"] == "owned-new-release"


@pytest.mark.parametrize("scenario", ["missing", "explicit"])
def test_portable_config_upgrade_preserves_values_and_is_idempotent(tmp_path, scenario):
    result = _run_source_harness(tmp_path, PORTABLE / "portable-agent.ps1", scenario, r'''
foreach ($Name in @("Install-TemporaryFile", "Update-PortableConfig")) {
    $Nodes = @($Ast.FindAll({ param($Node)
        $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and $Node.Name -eq $Name
    }, $true))
    if ($Nodes.Count -ne 1) { throw "Config function missing or ambiguous" }
    . ([ScriptBlock]::Create($Nodes[0].Extent.Text))
}
$DataDir = $Workspace
$ConfigPath = Join-Path $Workspace "config.json"
$Utf8NoBom = New-Object Text.UTF8Encoding($false)
$Initial = [ordered]@{allow_gpu_oc=$false; allow_miner_downloads=$false; miner_packages_dir="custom-packages"}
if ($Scenario -eq "explicit") {
    $Initial.managed_mining_enabled = $false
    $Initial.managed_miner_runtime_dir = "custom-runtime"
    $Initial.allow_agent_updates = $null
    $Initial.agent_update_max_mb = 128
    $Initial.agent_update_total_timeout = 45
}
$Initial.allow_wifi_management = $true
[IO.File]::WriteAllText($ConfigPath, ($Initial | ConvertTo-Json), $Utf8NoBom)
Update-PortableConfig
$After = [IO.File]::ReadAllText($ConfigPath)
Update-PortableConfig
[pscustomobject]@{
    config=($After | ConvertFrom-Json)
    unchanged_on_retry=($After -ceq [IO.File]::ReadAllText($ConfigPath))
    temporary_files=@(Get-ChildItem -LiteralPath $Workspace -Filter "*.tmp").Count
} | ConvertTo-Json -Depth 5 -Compress
''')
    config = result["config"]
    assert result["unchanged_on_retry"] and result["temporary_files"] == 0
    assert config["allow_gpu_oc"] is False and config["allow_miner_downloads"] is False
    assert config["miner_packages_dir"] == "custom-packages"
    assert "allow_wifi_management" not in config
    expected = {"managed_mining_enabled": True, "managed_miner_runtime_dir": "miner-runtime",
                "allow_agent_updates": True, "agent_update_max_mb": 512, "agent_update_total_timeout": 900}
    if scenario == "explicit":
        expected = {"managed_mining_enabled": False, "managed_miner_runtime_dir": "custom-runtime",
                    "allow_agent_updates": None, "agent_update_max_mb": 128, "agent_update_total_timeout": 45}
    assert {key: config[key] for key in expected} == expected


@pytest.mark.parametrize("mode", ["source", "frozen"])
@pytest.mark.parametrize("scenario", ["expected", "previous-python-command", "legacy", "legacy-blank", "legacy-incomplete", "foreign-path", "same-basename-foreign-path", "foreign-args", "foreign-working-directory", "foreign-description", "blank-description", "multiple-actions"])
def test_task_install_replaces_only_expected_or_owned_legacy_actions(tmp_path, mode, scenario):
    result = _run_source_harness(tmp_path, ROOT / "deploy/install-agent-task.ps1", mode + ":" + scenario, r'''
# Run only the real existing-task guard. No administrator checks, module load,
# task registration, process launch or SYSTEM task manipulation can execute.
$Guard = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$ExistingTask'
})
if ($Guard.Count -ne 1) { throw "Task ownership guard is missing or ambiguous" }
$Mode, $Case = $Scenario.Split(':')
$IsSourceInstall = $Mode -eq "source"
$PythonPath = "python.exe"
$TaskPath = "\"
$TaskName = "RigPilotAgent"
$KnownDescription = "RigPilot outbound-only device monitoring agent"
$Execute = Join-Path $Workspace $(if ($Mode -eq "source") { "python.exe" } else { "RigPilotAgent.exe" })
$Arguments = $(if ($Mode -eq "source") { '-m agent --config "test-config.json"' } else { '--config "test-config.json"' })
$WorkingDirectory = $Workspace
$ExpectedExistingAgentExePath = $null
$ExpectedExistingProjectRoot = $null
$ExpectedExistingPythonPath = $null
$ExpectedExistingConfigPath = $null
$ExistingTask = [pscustomobject]@{
    Description=$KnownDescription
    Actions=@([pscustomobject]@{Execute=$Execute; Arguments=$Arguments; WorkingDirectory=$WorkingDirectory})
}
switch ($Case) {
    "previous-python-command" { $ExistingTask.Actions[0].Execute = $PythonPath }
    { $_ -in @("legacy", "legacy-blank", "legacy-incomplete") } {
        $ExpectedExistingConfigPath = Join-Path $Workspace "legacy\config.json"
        $OldRoot = Join-Path $Workspace "legacy"
        if ($Mode -eq "source") {
            $ExpectedExistingProjectRoot = $OldRoot
            # Old classic source installs may have saved this exact command;
            # accepting it requires explicit root/config/command context.
            $ExpectedExistingPythonPath = "python.exe"
            $ExistingTask.Actions[0].Execute = $ExpectedExistingPythonPath
            $ExistingTask.Actions[0].Arguments = '-m agent --config "' + $ExpectedExistingConfigPath + '"'
        }
        else {
            $ExpectedExistingAgentExePath = Join-Path $OldRoot "RigPilotAgent.exe"
            $ExistingTask.Actions[0].Execute = $ExpectedExistingAgentExePath
            $ExistingTask.Actions[0].Arguments = '--config "' + $ExpectedExistingConfigPath + '"'
        }
        $ExistingTask.Actions[0].WorkingDirectory = $OldRoot
        if ($Case -eq "legacy-blank") { $ExistingTask.Description = "" }
        if ($Case -eq "legacy-incomplete") { $ExpectedExistingConfigPath = $null }
    }
    "foreign-path" { $ExistingTask.Actions[0].Execute = Join-Path $Workspace "OtherProduct.exe" }
    "same-basename-foreign-path" { $ExistingTask.Actions[0].Execute = Join-Path (Join-Path $Workspace "other") ([IO.Path]::GetFileName($Execute)) }
    "foreign-args" { $ExistingTask.Actions[0].Arguments = '--run-other-product' }
    "foreign-working-directory" { $ExistingTask.Actions[0].WorkingDirectory = Join-Path $Workspace "other" }
    "foreign-description" { $ExistingTask.Description = "Other product" }
    "blank-description" { $ExistingTask.Description = "" }
    "multiple-actions" { $ExistingTask.Actions += [pscustomobject]@{Execute=$Execute; Arguments=$Arguments; WorkingDirectory=$WorkingDirectory} }
}
$script:Stopped = $false
function Stop-ScheduledTask([string]$TaskPath, [string]$TaskName, [string]$ErrorAction) {
    $script:Stopped = $true
}
$Rejected = $false
try { . ([ScriptBlock]::Create($Guard[0].Extent.Text)) }
catch { $Rejected = $true }
[pscustomobject]@{rejected=$Rejected; stopped=$script:Stopped} | ConvertTo-Json -Compress
''')
    accepted = scenario in {"expected", "legacy", "legacy-blank"} or (mode == "source" and scenario == "previous-python-command")
    assert result["rejected"] is not accepted
    assert result["stopped"] is accepted


@pytest.mark.parametrize("mode", ["source", "frozen"])
@pytest.mark.parametrize("scenario", ["expected", "explicit-blank", "no-context", "same-basename-foreign-path", "foreign-config", "foreign-working-directory", "foreign-description", "blank-description", "multiple-actions"])
def test_classic_task_uninstall_requires_the_explicit_owned_action_context(tmp_path, mode, scenario):
    result = _run_source_harness(tmp_path, ROOT / "deploy/uninstall-agent-task.ps1", mode + ":" + scenario, r'''
$Guard = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$Task'
})
if ($Guard.Count -ne 1) { throw "Uninstall ownership guard is missing or ambiguous" }
$Mode, $Case = $Scenario.Split(':')
$TaskPath = "\"
$TaskName = "RigPilotAgent"
$KnownDescription = "RigPilot outbound-only device monitoring agent"
$ExpectedConfigPath = Join-Path $Workspace "config.json"
$ExpectedAgentExePath = $null
$ExpectedProjectRoot = $null
$ExpectedPythonPath = $null
$AllowLegacySourceTask = $false
$AllowLegacyEmptyDescription = $Case -eq "explicit-blank"
if ($Mode -eq "source") {
    $ExpectedProjectRoot = $Workspace
    $ExpectedPythonPath = "python.exe"
    $AllowLegacySourceTask = $true
    $Execute = $ExpectedPythonPath
    $Arguments = '-m agent --config "' + $ExpectedConfigPath + '"'
}
else {
    $ExpectedAgentExePath = Join-Path $Workspace "RigPilotAgent.exe"
    $Execute = $ExpectedAgentExePath
    $Arguments = '--config "' + $ExpectedConfigPath + '"'
}
$Task = [pscustomobject]@{Description=$KnownDescription; Actions=@([pscustomobject]@{
    Execute=$Execute; Arguments=$Arguments; WorkingDirectory=$Workspace
})}
switch ($Case) {
    "no-context" { $ExpectedConfigPath = $null; $ExpectedAgentExePath = $null; $ExpectedProjectRoot = $null; $ExpectedPythonPath = $null; $AllowLegacySourceTask = $false }
    "same-basename-foreign-path" { $Task.Actions[0].Execute = Join-Path (Join-Path $Workspace "other") ([IO.Path]::GetFileName($Execute)) }
    "foreign-config" { $Task.Actions[0].Arguments = $Arguments.Replace("config.json", "other.json") }
    "foreign-working-directory" { $Task.Actions[0].WorkingDirectory = Join-Path $Workspace "other" }
    "foreign-description" { $Task.Description = "Other product" }
    { $_ -in @("blank-description", "explicit-blank") } { $Task.Description = "" }
    "multiple-actions" { $Task.Actions += [pscustomobject]@{Execute=$Execute; Arguments=$Arguments; WorkingDirectory=$Workspace} }
}
$script:Stopped = $false
function Stop-ScheduledTask([string]$TaskPath, [string]$TaskName, [string]$ErrorAction) { $script:Stopped = $true }
$Rejected = $false
try { . ([ScriptBlock]::Create($Guard[0].Extent.Text)) } catch { $Rejected = $true }
[pscustomobject]@{rejected=$Rejected; stopped=$script:Stopped} | ConvertTo-Json -Compress
''')
    accepted = scenario in {"expected", "explicit-blank"}
    assert result["rejected"] is not accepted
    assert result["stopped"] is accepted


@pytest.mark.parametrize("scenario", ["valid", "pe32", "arm64", "bad-signature", "bad-mz", "short-dos", "short-optional-header", "negative-offset"])
def test_windows_builder_checks_pe32plus_and_length_before_native_execution(tmp_path, scenario):
    result = _run_source_harness(tmp_path, ROOT / "deploy/windows/build-windows-portable.ps1", scenario, r'''
$AgentHeader = New-Object byte[] 90
$AgentHeader[0]=0x4D; $AgentHeader[1]=0x5A
[BitConverter]::GetBytes([int]64).CopyTo($AgentHeader,0x3C)
[BitConverter]::GetBytes([uint32]0x00004550).CopyTo($AgentHeader,64)
[BitConverter]::GetBytes([uint16]0x8664).CopyTo($AgentHeader,68)
[BitConverter]::GetBytes([uint16]0x020B).CopyTo($AgentHeader,88)
switch ($Scenario) {
    "pe32" { [BitConverter]::GetBytes([uint16]0x010B).CopyTo($AgentHeader,88) }
    "arm64" { [BitConverter]::GetBytes([uint16]0xAA64).CopyTo($AgentHeader,68) }
    "bad-signature" { $AgentHeader[64]=0 }
    "bad-mz" { $AgentHeader[0]=0 }
    "short-dos" { [Array]::Resize([ref]$AgentHeader,63) }
    "short-optional-header" { [Array]::Resize([ref]$AgentHeader,89) }
    "negative-offset" { [BitConverter]::GetBytes([int]-1).CopyTo($AgentHeader,0x3C) }
}
$BuildFlows = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst] -and
    @($_.Body.Statements | Where-Object {
        $_ -is [Management.Automation.Language.AssignmentStatementAst] -and
        $_.Left.Extent.Text -eq '$AgentHeader'
    }).Count -eq 1
})
if ($BuildFlows.Count -ne 1) { throw "Actual owned build flow missing or ambiguous" }
$Checks = @($BuildFlows[0].Body.Statements | Where-Object {
    ($_ -is [Management.Automation.Language.IfStatementAst] -and
      ($_.Clauses[0].Item1.Extent.Text.StartsWith('$AgentHeader.Length') -or
       $_.Clauses[0].Item1.Extent.Text.StartsWith('$AgentPeOffset -lt'))) -or
    ($_ -is [Management.Automation.Language.AssignmentStatementAst] -and
      $_.Left.Extent.Text -eq '$AgentPeOffset')
})
if ($Checks.Count -ne 3) { throw "Actual PE header checks missing or ambiguous" }
$Rejected=$false
try { foreach ($Check in $Checks) { . ([ScriptBlock]::Create($Check.Extent.Text)) } }
catch { $Rejected=$true }
[pscustomobject]@{rejected=$Rejected} | ConvertTo-Json -Compress
''')
    assert result["rejected"] is (scenario != "valid")


def test_migration_preflight_and_rollback_have_operation_ownership_guards():
    script = (PORTABLE / "portable-migrate.ps1").read_text(encoding="utf-8")
    flow = script.rsplit("\ntry {\n", 1)[1]
    preflight = flow.split("$MutationStarted = $true", 1)[0]
    for guard in ("MigrationMutex.WaitOne", "Assert-PortableBundleManifest $SourceRoot",
                  "SourceIdentity.AgentId", "SourceIdentity.AgentSecret", "Get-CheckedTaskForRoot $TargetRoot"):
        assert guard in preflight
    rollback = flow.split("\ncatch {\n", 1)[1]
    assert rollback.index("if ($MutationStarted)") < rollback.index("Stop-AgentForRoot $TargetRoot")
    assert "if ($TargetTaskInstallAttempted -and -not $TargetTaskExisted)" in rollback
    assert "if ($TargetOverlayStarted)" in rollback
    assert "elseif ($TargetCreated -and" in rollback
    assert "$SourceAgentStopAttempted -and $SourceWasRunning" in rollback
    assert "$TargetAgentStopAttempted -and $TargetWasRunning" in rollback
    assert "if ($MinerStopAttempted)" in rollback
    assert "foreach ($TemporaryRoot in $OwnedTemporaryRoots)" in rollback
    assert "Select-Object -Reverse" not in script


@pytest.mark.parametrize("scenario", ["bad-manifest", "bad-source-path", "different-identity", "mutex-failure",
                                      "source-is-target", "target-task-collision", "target-recovery-task-collision"])
def test_migration_preflight_failure_does_not_touch_existing_target(tmp_path, scenario):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", scenario)
    assert result["exit_code"] == 1
    assert result["events"] == []
    assert result["source_running"] and result["target_running"] and result["target_task"]
    assert result["target_recovery_task"]
    assert result["target_exists"]
    for name in ("first.txt", "second.txt"):
        assert (tmp_path / "RigPilotAgentPortable" / name).read_text() == f"original-{name}"


@pytest.mark.parametrize("scenario", ["stop-source-failed", "stop-target-failed", "overlay-failed", "existing-target-install-failed"])
def test_migration_failed_mutation_restores_only_its_existing_target_changes(tmp_path, scenario):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", scenario)
    assert result["exit_code"] == 1
    assert result["source_running"] and result["target_running"] and result["target_task"]
    assert result["target_recovery_task"]
    assert result["target_exists"]
    assert "unregister:target" not in result["events"]
    if scenario == "stop-source-failed":
        assert result["events"] == ["stop:source", "start:source"]
    if scenario in {"overlay-failed", "existing-target-install-failed"}:
        assert [event for event in result["events"] if event.startswith("restore:")] == [
            "restore:second.txt", "restore:first.txt"
        ]
    for name in ("first.txt", "second.txt"):
        assert (tmp_path / "RigPilotAgentPortable" / name).read_text() == f"original-{name}"


@pytest.mark.parametrize("scenario", ["new-target-before-install-failed", "new-target-install-failed"])
def test_migration_rolls_back_its_new_target_and_only_a_task_install_it_attempted(tmp_path, scenario):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", scenario)
    assert result["exit_code"] == 1 and result["source_running"]
    assert not result["target_exists"] and not result["target_task"]
    assert not result["target_recovery_task"]
    assert ("unregister:target" in result["events"]) == (scenario == "new-target-install-failed")
    assert ("unregister:recovery" in result["events"]) == (scenario == "new-target-install-failed")
    assert (tmp_path / "source/identity.txt").read_text() == "source-identity"


def test_migration_rollback_preserves_an_existing_owned_recovery_task(tmp_path):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", "new-target-existing-recovery-install-failed")
    assert result["exit_code"] == 1 and result["source_running"]
    assert not result["target_exists"] and not result["target_task"]
    assert result["target_recovery_task"]
    assert "unregister:recovery" not in result["events"]


def test_migration_does_not_roll_back_a_target_created_by_another_operation(tmp_path):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", "new-target-raced")
    assert result["exit_code"] == 1 and result["source_running"]
    assert result["target_exists"] and result["target_running"] and result["target_task"]
    assert "stop:RigPilotAgentPortable" not in result["events"]
    assert "unregister:target" not in result["events"]
    assert (tmp_path / "RigPilotAgentPortable/identity.txt").read_text() == "unrelated-raced-identity"


@pytest.mark.parametrize("scenario", ["cleanup-failed", "rollback-restore-failed"])
def test_migration_cleanup_keeps_confirmed_target_and_failed_rollback_backups(tmp_path, scenario):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", scenario)
    backup = next(tmp_path.glob("RigPilotAgentPortable.rollback-*"))
    for name in ("first.txt", "second.txt"):
        assert (backup / name).read_text() == f"original-{name}"
        assert (tmp_path / "RigPilotAgentPortable" / name).read_text() == "changed"
    if scenario == "cleanup-failed":
        assert result["exit_code"] == 0 and result["target_running"] and result["target_task"]
        assert not any(event.startswith("restore:") for event in result["events"])
    else:
        assert result["exit_code"] == 1


def test_dotnet8_scope_stays_local_and_wraps_restore_and_publish():
    builder = (ROOT / "deploy/windows/build-windows-portable.ps1").read_text(encoding="utf-8")
    assert '(Join-Path $ScopeRoot "global.json")' in builder
    assert 'rollForward = "disable"' in builder
    assert 'allowPrerelease = $false' in builder
    assert "$DotNetSdkVersion -cne $SelectedSdkVersion" in builder
    assert builder.index("New-Item -ItemType Directory -Path $WorkRoot") < builder.index(
        "Initialize-DotNet8BuildScope $DotNetPath $WorkRoot"
    )
    inputs = builder.split("$CpuTemperatureSource =", 1)[1].split("$CpuTemperaturePublish =", 1)[0]
    assert 'Join-Path $WorkRoot "helper-source"' in inputs
    assert 'Join-Path $CpuTemperatureBuildSource "RigPilot.CpuTemperature.csproj"' in inputs
    declared = re.search(r"foreach \(\$InputName in @\(([^)]+)\)\)", inputs).group(1)
    assert re.findall(r'"([^"]+)"', declared) == ["RigPilot.CpuTemperature.csproj", "Program.cs", "packages.lock.json"]
    assert "[IO.File]::Copy(" in inputs
    assert "(Join-Path $CpuTemperatureSource $InputName)" in inputs
    assert "(Join-Path $CpuTemperatureBuildSource $InputName)" in inputs
    publish = builder.split("$CpuTemperaturePublish =", 1)[1].split("$CpuTemperatureExe =", 1)[0]
    assert publish.index("Push-Location -LiteralPath $WorkRoot") < publish.index("& $DotNetPath restore")
    assert publish.index("& $DotNetPath publish") < publish.index("finally {\n    Pop-Location")
    assert "--locked-mode" in publish and "-r win-x64" in publish and "--no-restore" in publish


def test_dotnet8_inventory_pattern_excludes_other_majors_and_prereleases():
    # Verify the actual listing pattern without inventing a PowerShell runner.
    # Native tests below execute selection, scope handling and error paths when
    # PowerShell is installed; this small regex check runs on every host.
    builder = (ROOT / "deploy/windows/build-windows-portable.ps1").read_text(encoding="utf-8")
    pattern = re.search(r"if \(\[string\]\$SdkLine -match '([^']+)'\)", builder).group(1)
    lines = ["8.0.99 [C:\\sdk]", "9.0.300 [C:\\sdk]", "8.0.422 [C:\\sdk]",
             "8.0.500-preview.1 [C:\\sdk]", "7.0.400 [C:\\sdk]", "invalid"]
    versions = [match.group(1) for line in lines if (match := re.search(pattern, line))]
    assert versions == ["8.0.99", "8.0.422"]
    assert max(versions, key=lambda version: tuple(map(int, version.split(".")))) == "8.0.422"


@pytest.mark.parametrize("scenario", ["mixed-sdks", "missing-sdk8", "list-failure", "resolver-mismatch"])
def test_dotnet8_selection_and_publish_use_the_same_owned_scope(tmp_path, scenario):
    result = _run_review_fixture(tmp_path, "windows_sdk_scope_review.ps1",
                                 ROOT / "deploy/windows/build-windows-portable.ps1", scenario)
    assert result["cwd_restored"]
    assert result["source_unchanged"] and not result["source_has_obj"]
    if scenario == "mixed-sdks":
        assert result["failure"] is None and result["selected"] == "8.0.422"
        assert result["selection"] == {"sdk": {"version": "8.0.422", "rollForward": "disable", "allowPrerelease": False}}
        assert [call["command"] for call in result["calls"]] == ["--version", "restore", "publish"]
        assert all(call["version"] == "8.0.422" for call in result["calls"])
        assert all(Path(call["cwd"]) == tmp_path / "owned-sdk-scope" for call in result["calls"])
        assert result["copied_hashes"] == result["input_hashes"]
        assert set(result["input_hashes"]) == {"RigPilot.CpuTemperature.csproj", "Program.cs", "packages.lock.json"}
        for call in result["calls"][1:]:
            assert Path(call["project"]) == tmp_path / "owned-sdk-scope/helper-source/RigPilot.CpuTemperature.csproj"
            assert Path(call["project_scope"]) == tmp_path / "owned-sdk-scope"
        assert (tmp_path / "owned-sdk-scope/helper-source/obj/restore.fake").is_file()
        assert (tmp_path / "owned-sdk-scope/helper-source/obj/publish.fake").is_file()
    else:
        assert result["failure"]
        assert not any(call["command"] in {"restore", "publish"} for call in result["calls"])
        if scenario in {"missing-sdk8", "list-failure"}:
            assert result["selection"] is None


def test_portable_launcher_installs_no_service_and_keeps_token_out_of_config():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    config_block = runner.split("$Config = [ordered]@{", 1)[1].split(
        "$Json =", 1
    )[0]

    assert 'Read-Host $Prompt -AsSecureString' in runner
    assert '$Variable, $Token' in runner
    assert '"RIGPILOT_ENROLLMENT_TOKEN", $null' in runner
    assert '"RIGPILOT_REPLACEMENT_TOKEN", $null' in runner
    assert "enrollment_token" not in config_block
    assert "replacement_token" not in config_block
    assert "CommonApplicationData" in runner
    assert "$env:ProgramData" not in runner
    assert "New-Service" not in runner
    assert "CreateService" not in runner
    assert "sc.exe create" not in runner.lower()
    assert '-Verb RunAs' in runner
    assert "Test-IsAdministrator" in runner
    assert "Restart-Elevated" in runner
    assert '"CheckConnection"' in runner


def test_portable_launcher_uses_relative_data_and_safe_process_identity():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")

    for value in (
        'state_file = "state.json"',
        'log_file = "logs\\agent.jsonl"',
        'flight_sheet_file = "flight-sheet.json"',
        'miner_status_file = "miner-status.json"',
        "managed_mining_enabled = $true",
        'managed_miner_runtime_dir = "miner-runtime"',
        'miner_packages_dir = "packages"',
        'miner_profile_allowlist = @("default")',
        "allow_miner_downloads = $true",
        "allow_gpu_oc = $true",
        "allow_reboot = $true",
        "allow_shutdown = $true",
        "allow_agent_updates = $true",
        "agent_update_max_mb = 512",
        'gpu_oc_allowed_vendors = @("nvidia")',
    ):
        assert value in runner

    assert "$PidPath" in runner
    assert "executable_path" in runner
    assert "started_at" in runner
    assert "$StartDelta -lt 1" in runner
    assert "ReparsePoint" in runner
    assert 'Global\\RigPilotAgentPortableLaunch-$PortableIdentity' in runner
    assert "Enter-PortableLaunchMutex" in runner
    assert "Exit-PortableLaunchMutex" in runner


def test_portable_autostart_is_optional_elevated_and_idempotent():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")

    assert '"InstallAutostart", "RemoveAutostart", "CheckConnection"' in runner
    assert "EnablePowerActions" not in runner
    assert "[switch]$BootstrapEnrollment" in runner
    assert "[switch]$Scheduled" in runner
    assert "New-ScheduledTaskTrigger -AtStartup" in runner
    assert '-UserId "SYSTEM"' in runner
    assert "-LogonType ServiceAccount" in runner
    assert "-RunLevel Highest" in runner
    assert "-MultipleInstances IgnoreNew" in runner
    assert "Register-ScheduledTask -TaskName $AutostartTaskName" in runner
    assert "-InputObject $Task -Force" in runner
    assert "Unregister-ScheduledTask -TaskName $AutostartTaskName" in runner
    assert "Test-OwnedAutostartTask" in runner
    assert "Test-SystemTaskPrincipal" in runner
    assert '"S-1-5-18"' in runner
    assert '-TaskPath "\\"' in runner
    assert "complete enrollment before installing autostart" in runner
    assert "Initialize-PortableEnrollment $false" in runner
    assert ' -BootstrapEnrollment' in runner
    assert '" -Action Start -Scheduled' in runner
    assert "$ActiveAgentPointerRepairNeeded" in runner
    assert 'Remove-Item -LiteralPath $BrokenPointer -Force' in runner
    assert 'if ($Action -eq "InstallAutostart")' not in runner.split(
        "$AgentExe = $null", 1
    )[1].split("function Test-IsAdministrator", 1)[0]


def test_legacy_autostart_migrates_to_the_fixed_protected_root():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    migrator = (PORTABLE / "portable-migrate.ps1").read_text(encoding="utf-8")
    builder = (
        ROOT / "deploy" / "windows" / "build-windows-portable.ps1"
    ).read_text(encoding="utf-8")

    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]
    assert "Invoke-PortableMigration" in installer
    assert installer.index("Invoke-PortableMigration") < installer.index(
        "Protect-PortableUpdateRoot"
    )
    assert '"portable-migrate.ps1"' in builder
    assert "Register-ScheduledTask" not in migrator
    assert "CommonApplicationData" in migrator
    assert "$env:ProgramData" not in migrator
    assert "$ProgramDataRoot" in migrator
    assert "Test-OwnedTaskForRoot" in migrator
    assert "AgentId" in migrator and "AgentSecret" in migrator
    assert "Apply-ProgramOverlay $SourceRoot $TargetRoot" in migrator
    assert "Get-PortableManifestProgramFiles $From" in migrator
    assert "Get-PortableManifestProgramFiles $To" in migrator
    assert "Portable program manifest cannot include data or versions" in migrator
    assert "$Manifest.schema_version -is [bool]" in migrator
    assert '$First, "data"' in migrator
    assert '$First, "versions"' in migrator
    assert "[IO.Path]::IsPathRooted($Relative)" in migrator
    assert "Remove-UnmanifestedProgramFiles $TargetRoot" not in migrator
    assert "Enable-Power-Actions.bat" in migrator
    assert "Test-SystemTaskPrincipal" in migrator
    assert "SourceIdentity.AgentId" in migrator
    assert "TargetIdentity.AgentId" in migrator
    assert "SourceIdentity.AgentSecret" in migrator
    assert "TargetIdentity.AgentSecret" in migrator
    assert "belongs to a different device identity. Nothing was changed" in migrator
    assert "Assert-PlainPathComponents" in migrator
    assert "Assert-PlainTree" in migrator
    assert "Assert-PortableBundleManifest" in migrator
    assert "Global\\RigPilotAgentPortableMigrationV1" in migrator
    assert "Remove-CheckedTree $SourceRoot" not in migrator
    assert "The old portable folder and its data were kept" in migrator


def test_install_autostart_requires_a_valid_enrollment_identity_before_migration():
    """Both Install-Autostart branches preflight the identity with a message a user can act on."""
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    migrator = (PORTABLE / "portable-migrate.ps1").read_text(encoding="utf-8")

    preflight = runner.split("function Assert-PortableEnrolledIdentityReady", 1)[1].split(
        "function Install-PortableAutostart", 1
    )[0]
    assert "Run Start-RigPilotAgent.bat once and complete enrollment before installing autostart." in preflight
    assert "Portable device state has no valid enrollment identity" in preflight
    assert "Test-PortableEnrolledIdentityFile" in preflight
    # The launcher's validity criteria stay identical to the coordinator's.
    coordinator_identity = migrator.split("function Read-EnrollmentIdentity", 1)[1].split(
        "function Assert-PortableBundleManifest", 1
    )[0]
    launcher_identity = runner.split("function Test-PortableEnrolledIdentityFile", 1)[1].split(
        "function Get-PortableLogTail", 1
    )[0]
    agent_id_pattern = r"'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$'"
    assert agent_id_pattern in coordinator_identity
    assert agent_id_pattern in launcher_identity
    assert "$AgentSecret.Length -gt 2048" in coordinator_identity
    assert "$AgentSecret.Length -gt 2048" in launcher_identity

    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]
    migration_branch = installer.split("Invoke-PortableMigration", 1)[0]
    assert "Assert-PortableEnrolledIdentityReady" in migration_branch
    assert installer.index("Assert-PortableEnrolledIdentityReady") < installer.index(
        "Invoke-PortableMigration"
    )
    # The protected-root branch keeps the same preflight before any mutation.
    protected_branch = installer.split("Invoke-PortableMigration", 1)[1]
    assert "Assert-PortableEnrolledIdentityReady" in protected_branch
    assert protected_branch.index("Assert-PortableEnrolledIdentityReady") < protected_branch.index(
        "Protect-PortableUpdateRoot"
    )


def test_migration_failure_reasons_reach_the_parent_console_and_a_durable_log():
    """A failed migration must explain itself in the parent console and on disk."""
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    migrator = (PORTABLE / "portable-migrate.ps1").read_text(encoding="utf-8")

    launcher_migration = runner.split("function Invoke-PortableMigration", 1)[1].split(
        "function Write-PortableUpdateReadyMarker", 1
    )[0]
    assert "-NoNewWindow" in launcher_migration
    assert "portable-migration.log" in launcher_migration
    assert "Get-PortableLogTail $MigrationLogPath 12" in launcher_migration
    assert "The old folder was kept." in launcher_migration

    # The coordinator durably records its outcome beside the source data.
    assert migrator.count("function Write-MigrationOutcomeLog") == 1
    flow = migrator.rsplit("\ntry {\n", 1)[1]
    success_flow = flow.split("\ncatch {\n", 1)[0]
    failure_flow = flow.split("\ncatch {\n", 1)[1]
    assert 'Write-MigrationOutcomeLog ("succeeded: migrated to " + $TargetRoot)' in success_flow
    assert 'Write-MigrationOutcomeLog ("failed: " + $Failure)' in failure_flow
    assert "portable-migration.log" in migrator.split(
        "function Write-MigrationOutcomeLog", 1
    )[1].split("function Get-MigrationLogTail", 1)[0]

    # A failed protected-target install surfaces the target launcher's own log.
    target_install = success_flow.split("$TargetTaskInstallAttempted = $true", 1)[1].split(
        "$VerifiedTargetTask", 1
    )[0]
    assert "-NoNewWindow" in target_install
    assert 'logs\\portable-launcher.log' in target_install
    assert "Get-MigrationLogTail $TargetLauncherLog 6" in target_install

    # Every launcher failure is appended to its own durable log for support.
    main_catch = runner.rsplit("\ncatch {\n", 1)[1]
    assert "Write-PortableLauncherFailureLog $FailureMessage" in main_catch
    launcher_log_helper = runner.split("function Write-PortableLauncherFailureLog", 1)[1].split(
        "function Assert-PortableEnrolledIdentityReady", 1
    )[0]
    assert 'portable-launcher.log' in launcher_log_helper
    assert "1048576" in launcher_log_helper


@pytest.mark.parametrize("scenario", ["bad-manifest", "stop-source-failed"])
def test_migration_failure_writes_the_durable_outcome_log(tmp_path, scenario):
    result = _run_review_fixture(tmp_path, "windows_migration_review.ps1",
                                 PORTABLE / "portable-migrate.ps1", scenario)
    assert result["exit_code"] == 1
    log = tmp_path / "source" / "data" / "logs" / "portable-migration.log"
    assert log.is_file(), "the coordinator must append its failure reason beside the source data"
    text = log.read_text(encoding="utf-8")
    assert re.match(r"^\d{4}-\d{2}-\d{1,2}T", text), "each outcome line carries a UTC timestamp"
    assert "failed: " in text
    if scenario == "bad-manifest":
        assert "Bad source manifest" in text
    else:
        assert "Source stop failed" in text


def test_install_autostart_failure_keeps_enrolled_device_online_in_foreground():
    """A failed migration/autostart install must not strand an enrolled device offline."""
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")

    helper = runner.split("function Invoke-PortableForegroundAgentRun", 1)[1].split(
        "\ntry {", 1
    )[0]
    for guard in (
        "Enter-PortableLaunchMutex",
        "Get-OwnedAgentProcesses",
        "Initialize-PortableEnrollment $ScheduledMode",
        "$AgentProcess.WaitForExit()",
        "return $AgentExitCode",
    ):
        assert guard in helper
    assert "'--config \"' + $ConfigPath + '\"'" in helper

    # The Start action reuses the same shared foreground run.
    start_action = runner.rsplit('if ($Action -eq "Stop")', 1)[1]
    assert "exit (Invoke-PortableForegroundAgentRun ([bool]$Scheduled))" in start_action
    assert "Initialize-PortableEnrollment ([bool]$Scheduled)" not in start_action

    # Install-Autostart falls back to that run only when a complete enrolled
    # identity exists and no Agent is already running; otherwise it rethrows.
    install_action = runner.split('if ($Action -eq "InstallAutostart") {', 1)[1].split(
        'if ($Action -eq "RemoveAutostart")', 1
    )[0]
    assert install_action.count("Install-PortableAutostart") == 1
    tail = install_action.split("Install-PortableAutostart", 1)[1]
    assert tail.index("catch {") < tail.index("Test-PortableEnrolledIdentityFile")
    assert tail.index("Test-PortableEnrolledIdentityFile") < tail.index(
        "Invoke-PortableForegroundAgentRun $false"
    )
    assert "Get-OwnedAgentProcesses).Count -gt 0" in tail
    assert "keeping the enrolled device online instead" in tail
    assert "Invoke-PortableForegroundAgentRun $false | Out-Null" in tail
    assert "if ($MigrationInstall -or" in tail
    assert tail.index("Write-PortableLauncherFailureLog $AutostartFailure") < tail.index(
        "Invoke-PortableForegroundAgentRun $false"
    )
    assert "exit 1" in tail


@pytest.mark.parametrize("scenario", [
    "interactive", "interactive-agent-failed", "migration-child", "identity-missing",
    "already-running", "protected-running", "success",
])
def test_autostart_failure_returns_to_migration_or_preserves_interactive_failure(tmp_path, scenario):
    result = _run_source_harness(tmp_path, PORTABLE / "portable-agent.ps1", scenario, r'''
$Flow = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst]
})[-1]
$Install = @($Flow.Body.Statements | Where-Object {
    $_ -is [Management.Automation.Language.IfStatementAst] -and
    $_.Clauses[0].Item1.Extent.Text -eq '$Action -eq "InstallAutostart"'
})
if ($Install.Count -ne 1) { throw "Install action is missing" }
$Attempt = @($Install[0].Clauses[0].Item2.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst]
})
if ($Attempt.Count -ne 1) { throw "Install attempt is missing" }
$Logger = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Write-PortableLauncherFailureLog"
}, $true))[0]
. ([ScriptBlock]::Create($Logger.Extent.Text))
$Action = "InstallAutostart"
$DataDir = Join-Path $Workspace "data"
[void][IO.Directory]::CreateDirectory($DataDir)
$ConfigPath = Join-Path $DataDir "config.json"
[IO.File]::WriteAllText($ConfigPath, "{}")
$MigrationInstall = $Scenario -eq "migration-child"
$script:ForegroundCalls = 0
$script:ExitCode = 0
$Caught = $null
function Install-PortableAutostart {
    if ($Scenario -ne "success") { throw "synthetic scheduler registration failure" }
}
function Test-PortableEnrolledIdentityFile { return $Scenario -ne "identity-missing" }
function Get-OwnedAgentProcesses {
    if ($Scenario -eq "already-running") { [pscustomobject]@{ ProcessId=4242 } }
}
function Test-PortableProtectedAgentRunning { return $Scenario -eq "protected-running" }
function Invoke-PortableForegroundAgentRun([bool]$ScheduledMode) {
    $script:ForegroundCalls++
    if ($ScheduledMode) { throw "Fallback cannot impersonate a scheduled run" }
    if ($Scenario -eq "interactive-agent-failed") { return 23 }
    return 0
}
$AttemptText = $Attempt[0].Extent.Text.Replace('exit 1', '$script:ExitCode = 1; return')
try { & ([ScriptBlock]::Create($AttemptText)) } catch {
    $Caught = $_.Exception.Message
    $script:ExitCode = 1
}
$Log = Join-Path $DataDir "logs\portable-launcher.log"
[pscustomobject]@{
    exit_code=$script:ExitCode
    foreground_calls=$script:ForegroundCalls
    caught=$Caught
    log=$(if (Test-Path -LiteralPath $Log) { [IO.File]::ReadAllText($Log) } else { "" })
} | ConvertTo-Json -Compress
''')
    interactive = scenario in {"interactive", "interactive-agent-failed"}
    assert result["foreground_calls"] == (1 if interactive else 0)
    assert result["exit_code"] == (0 if scenario == "success" else 1)
    if interactive:
        assert result["caught"] is None
        assert "synthetic scheduler registration failure" in result["log"]
    elif scenario == "success":
        assert result["caught"] is None and result["log"] == ""
    else:
        assert result["caught"] == "synthetic scheduler registration failure"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Task Scheduler CIM construction")
def test_autostart_task_has_canonical_unlimited_execution_time(tmp_path):
    result = _run_source_harness(tmp_path, PORTABLE / "portable-agent.ps1", "autostart", r'''
# Load native CIM constructors BEFORE installing mocks. An Import-Module in
# the source function otherwise creates function-local proxies that shadow
# script-scope mocks and can register/run a real task.
Microsoft.PowerShell.Core\Import-Module ScheduledTasks -ErrorAction Stop
$Function = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Install-PortableAutostart"
}, $true))[0]
. ([ScriptBlock]::Create($Function.Extent.Text))
$PortableRoot = Join-Path $Workspace "RigPilotAgentPortable"
$DataDir = Join-Path $PortableRoot "data"
$ConfigPath = Join-Path $DataDir "config.json"
$StatePath = Join-Path $DataDir "state.json"
$AutostartTaskName = "RigPilotSyntheticAutostart"
$RecoveryTaskName = "RigPilotSyntheticRecovery"
$ActiveAgentPointerRepairNeeded = $false
$script:Registered = $null
$script:Events = @()
$script:QueryCalls = 0
$script:ImportCalls = 0
function Import-Module([string]$Name, [string]$ErrorAction) {
    if ($Name -ne "ScheduledTasks") { throw "Unexpected module request" }
    $script:ImportCalls++
}
function Get-TrustedProgramDataRoot { return $Workspace }
function Assert-PortableEnrolledIdentityReady { }
function Assert-NotReparsePoint { }
function Get-ScheduledTask { $script:QueryCalls++; return $script:Registered }
function Protect-PortableUpdateRoot { }
function Set-PortablePowerActions { return $true }
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $InputObject, [switch]$Force)
    $script:Registered = $InputObject
    $script:Events += "register"
}
function Test-OwnedAutostartTask($Task) { return $null -ne $Task }
function Install-PortableRecoveryWatchdog { $script:Events += "recovery" }
function Start-ScheduledTask { $script:Events += "start" }
function Write-PortableUpdateReadyMarker { $script:Events += "ready" }
foreach ($Name in @("Import-Module", "Get-ScheduledTask", "Register-ScheduledTask", "Start-ScheduledTask")) {
    $Command = Get-Command $Name -CommandType Function -ErrorAction Stop
    if ($Command.ModuleName) { throw "Unsafe native command escaped the fixture: $Name" }
}
Install-PortableAutostart
[pscustomobject]@{
    execution_limit=[string]$script:Registered.Settings.ExecutionTimeLimit
    principal=[string]$script:Registered.Principal.UserId
    events=@($script:Events)
    query_calls=$script:QueryCalls
    import_calls=$script:ImportCalls
} | ConvertTo-Json -Compress
''')
    assert result["execution_limit"] == "PT0S"
    assert result["principal"] in {"SYSTEM", "S-1-5-18"}
    assert result["events"] == ["register", "recovery", "start", "ready"]
    assert result["query_calls"] == 2
    assert result["import_calls"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Native Windows process path semantics")
@pytest.mark.parametrize("scenario", ["root", "slot", "sibling-prefix", "other-program"])
def test_foreground_fallback_recognizes_only_protected_agent_paths(tmp_path, scenario):
    result = _run_source_harness(tmp_path, PORTABLE / "portable-agent.ps1", scenario, r'''
$Function = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Test-PortableProtectedAgentRunning"
}, $true))[0]
. ([ScriptBlock]::Create($Function.Extent.Text))
$Protected = Join-Path $Workspace "RigPilotAgentPortable"
$script:ProcessPath = switch ($Scenario) {
    "root" { Join-Path $Protected "RigPilotAgent.exe" }
    "slot" { Join-Path $Protected "versions\0.8.60\RigPilotAgent.exe" }
    "sibling-prefix" { Join-Path ($Protected + "-other") "RigPilotAgent.exe" }
    "other-program" { Join-Path $Protected "versions\0.8.60\Other.exe" }
}
function Get-TrustedProgramDataRoot { return $Workspace }
function Get-WindowsProcessInfo { [pscustomobject]@{ ExecutablePath=$script:ProcessPath } }
[pscustomobject]@{ running=[bool](Test-PortableProtectedAgentRunning) } | ConvertTo-Json -Compress
''')
    assert result["running"] is (scenario in {"root", "slot"})


@pytest.mark.skipif(os.name != "nt", reason="Native Windows updater process entrypoint")
def test_recovery_script_dispatches_without_activation_parameters(tmp_path):
    # The actual script is invoked, not just its extracted recovery function.
    # A deliberately non-production root must reach its explicit guard without
    # initializing empty Activate paths or touching any real installation.
    completed = subprocess.run(
        [_review_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(PORTABLE / "RigPilotAgentUpdater.ps1"),
         "-PortableRoot", str(tmp_path), "-Action", "Recover"],
        capture_output=True, text=True, encoding="utf-8", timeout=20, check=False,
    )
    assert completed.returncode == 1
    assert any(message in completed.stdout for message in (
        "requires the fixed protected ProgramData root",
        "requires an administrator token",
    )), completed.stdout + completed.stderr
    assert "GetFullPath" not in completed.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Native Windows Task Scheduler CIM construction")
@pytest.mark.parametrize("script_name", ["portable-agent.ps1", "RigPilotAgentUpdater.ps1"])
def test_recovery_task_uses_unbounded_repeat_without_oversized_duration(tmp_path, script_name):
    result = _run_source_harness(tmp_path, PORTABLE / script_name, "recovery", r'''
Microsoft.PowerShell.Core\Import-Module ScheduledTasks -ErrorAction Stop
$Function = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Install-PortableRecoveryWatchdog"
}, $true))[0]
. ([ScriptBlock]::Create($Function.Extent.Text))
$PortableRoot = $Workspace
$PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$RecoveryTaskName = "RigPilotSyntheticRecovery"
[IO.File]::WriteAllText((Join-Path $PortableRoot "RigPilotAgentUpdater.ps1"), "# synthetic")
$script:Registered = $null
$script:RegisterCalls = 0
$script:QueryCalls = 0
$script:ImportCalls = 0
function Import-Module([string]$Name, [string]$ErrorAction) {
    if ($Name -ne "ScheduledTasks") { throw "Unexpected module request" }
    $script:ImportCalls++
}
function Get-ScheduledTask { $script:QueryCalls++; return $script:Registered }
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $InputObject, [switch]$Force)
    $script:RegisterCalls++
    $script:Registered = $InputObject
}
function Start-ScheduledTask { throw "Recovery construction must never start a task" }
function Test-OwnedRecoveryTask($Task) { return $null -ne $Task }
# Import and all task query/mutation operations are mocked. Only the
# New-ScheduledTask* constructors use the real Windows CIM provider.
foreach ($Name in @("Import-Module", "Get-ScheduledTask", "Register-ScheduledTask", "Start-ScheduledTask")) {
    $Command = Get-Command $Name -CommandType Function -ErrorAction Stop
    if ($Command.ModuleName) { throw "Unsafe native command escaped the fixture: $Name" }
}
Install-PortableRecoveryWatchdog
$Repeat = @($script:Registered.Triggers | Where-Object { $_.Repetition.Interval })[0]
[pscustomobject]@{
    repeat_interval=[string]$Repeat.Repetition.Interval
    repeat_duration=[string]$Repeat.Repetition.Duration
    execution_limit=[string]$script:Registered.Settings.ExecutionTimeLimit
    principal=[string]$script:Registered.Principal.UserId
    triggers=@($script:Registered.Triggers).Count
    register_calls=$script:RegisterCalls
    query_calls=$script:QueryCalls
    import_calls=$script:ImportCalls
} | ConvertTo-Json -Compress
''')
    assert result["repeat_interval"] == "PT1M"
    assert result["repeat_duration"] == ""
    assert result["execution_limit"] == "PT10M"
    assert result["principal"] in {"SYSTEM", "S-1-5-18"}
    assert result["triggers"] == 2
    assert result["register_calls"] == 1
    assert result["query_calls"] == 2
    assert result["import_calls"] == 1


def test_migration_stops_and_resumes_only_the_verified_managed_miner():
    migrator = (PORTABLE / "portable-migrate.ps1").read_text(encoding="utf-8")

    assert "Test-LiveManagedMiner" in migrator
    assert "managed-miner-process.json" in migrator
    assert "Record.executable" in migrator
    assert "Record.create_time" in migrator
    assert '"--stop-managed-mining-once"' in migrator
    assert '"--resume-managed-mining-once"' in migrator
    assert 'Invoke-ManagedMinerOnce $TargetRoot $TargetRoot "Resume"' in migrator
    assert "Wait-AgentForRoot $TargetRoot" in migrator
    assert "agent-update-ready.json" in migrator
    assert "Ignoring a stale managed miner PID record during migration" in migrator
    # Windows PowerShell variable names are case-insensitive; $PID is a
    # read-only automatic variable, so even assigning to `$Pid` aborts.
    assert "$Pid =" not in migrator


def test_task_collision_preflight_happens_before_acl_mutation():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]

    assert installer.index("Test-OwnedAutostartTask") < installer.index(
        "Protect-PortableUpdateRoot"
    )


def test_same_identity_target_overlay_is_data_preserving_and_repairable():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    migrator = (PORTABLE / "portable-migrate.ps1").read_text(encoding="utf-8")

    overlay = migrator.split("function Apply-ProgramOverlay", 1)[1].split(
        "function Undo-ProgramOverlay", 1
    )[0]
    assert "Get-PortableManifestProgramFiles $From" in overlay
    assert "Get-PortableManifestProgramFiles $To" in overlay
    assert "$RetiredFiles" in overlay
    assert 'foreach ($ExplicitRetired in @("Enable-Power-Actions.bat"))' in overlay
    assert "Join-CheckedTreePath $To $RetiredRelative" in overlay
    assert "OverlayRecords" in overlay
    assert "Remove-Item -LiteralPath $Destination -Force" in overlay
    assert "Get-PlainTreeEntries $From $true" not in overlay
    assert "SourceIdentity.AgentId" in migrator
    assert "TargetIdentity.AgentId" in migrator
    assert "SourceIdentity.AgentSecret" in migrator
    assert "TargetIdentity.AgentSecret" in migrator
    assert "belongs to a different device identity. Nothing was changed" in migrator
    assert "Ignoring a stale managed miner PID record during migration" in migrator

    resolver = runner.split("$AgentExe = $null", 1)[1].split(
        "function Test-IsAdministrator", 1
    )[0]
    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]
    assert '$Action -eq "InstallAutostart"' in resolver
    assert "$ActiveAgentPointerRepairNeeded = $true" in resolver
    assert "Remove-Item -LiteralPath $BrokenPointer -Force" in installer
    assert installer.index("Protect-PortableUpdateRoot") < installer.index(
        "Remove-Item -LiteralPath $BrokenPointer -Force"
    )


def test_existing_portable_config_has_an_explicit_power_action_opt_in():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    setter = runner.split("function Set-PortablePowerActions", 1)[1].split(
        "function Invoke-Enrollment", 1
    )[0]
    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]

    assert '"allow_reboot", "allow_shutdown"' in setter
    assert "$Property.Value = $true" not in setter
    assert "-NotePropertyName $PropertyName" in setter
    assert "-NotePropertyValue $true" in setter
    assert "-Force" in setter
    assert "ConvertTo-Json -Depth 8" in setter
    assert "Assert-NotReparsePoint $ConfigPath" in setter
    assert "Set-PortablePowerActions -RequireConfig $true" in installer
    assert installer.index("Protect-PortableUpdateRoot") < installer.index(
        "Set-PortablePowerActions -RequireConfig $true"
    ) < installer.index("Register-ScheduledTask")
    assert "Enable-PortablePowerActions" not in runner


def test_install_autostart_is_the_combined_power_setup_entrypoint():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    install_batch = (PORTABLE / "Install-Autostart.bat").read_text(encoding="utf-8")
    start_batch = (PORTABLE / "Start-RigPilotAgent.bat").read_text(encoding="utf-8")
    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]

    assert "Set-PortablePowerActions -RequireConfig $true" in installer
    assert installer.index("Set-PortablePowerActions -RequireConfig $true") < installer.index(
        "Start-ScheduledTask"
    )
    assert 'call "%~dp0Start-RigPilotAgent.bat" --install-autostart' in install_batch
    assert '"%~dp0portable-entry.ps1" -Action InstallAutostart' in start_batch
    assert "-Action InstallAutostart -BootstrapEnrollment" not in start_batch
    assert 'if /I not "%~1"=="--install-autostart"' in start_batch
    bootstrap = runner.split('if ($Action -eq "InstallAutostart")', 1)[1].split(
        "Install-PortableAutostart", 1
    )[0]
    assert "$EnrollmentRequired" in bootstrap
    assert "Test-Path -LiteralPath $ConfigPath -PathType Leaf" in bootstrap
    assert "Test-Path -LiteralPath $StatePath -PathType Leaf" in bootstrap
    assert "if ($BootstrapEnrollment -or $EnrollmentRequired)" in bootstrap
    assert "Initialize-PortableEnrollment $false" in bootstrap


def test_mutable_start_batch_uses_only_the_legacy_trusted_launcher_interface():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    entry = (PORTABLE / "portable-entry.ps1").read_text(encoding="utf-8")
    start_batch = (PORTABLE / "Start-RigPilotAgent.bat").read_text(encoding="utf-8")

    # Remote updates retain the trusted launcher/updater pair but synchronize
    # batch files from the new release. The mutable batch must therefore use
    # only an action understood by the oldest update-capable launcher.
    assert '"%~dp0portable-entry.ps1" -Action InstallAutostart' in start_batch
    assert "-BootstrapEnrollment" not in start_batch
    assert 'Join-Path $PortableRoot "portable-agent.ps1"' in entry
    assert "-Action $Action" in entry
    assert "[switch]$Scheduled" not in entry
    install_branch = runner.split('if ($Action -eq "InstallAutostart")', 1)[1].split(
        "Install-PortableAutostart", 1
    )[0]
    assert "$EnrollmentRequired" in install_branch
    assert "Initialize-PortableEnrollment $false" in install_branch


def test_portable_uses_windows_powershell_51_safe_elevation_and_file_replacement():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    elevation = runner.split("function Restart-Elevated", 1)[1].split(
        "function Assert-NotReparsePoint", 1
    )[0]
    replacement = runner.split("function Install-TemporaryFile", 1)[1].split(
        "function Get-WindowsProcessInfo", 1
    )[0]

    assert "$ElevationArguments" in elevation
    assert "-ArgumentList $ElevationArguments" in elevation
    assert 'System32\\WindowsPowerShell\\v1.0\\powershell.exe' in elevation
    assert "$Elevated.Refresh()" in elevation
    assert 'if ($RequestedAction -eq "Start")' not in elevation
    assert "[IO.File]::Replace" in replacement
    assert "$BackupPath" in replacement
    assert "$null, $true" not in replacement
    assert "Move-Item -LiteralPath" not in replacement
    assert "Move-Item -LiteralPath $TemporaryConfig" not in runner


def test_portable_stop_has_win10_process_fallbacks_and_is_idempotent():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    process_helper = runner.split("function Get-WindowsProcessInfo", 1)[1].split(
        "function Get-PortableIdentity", 1
    )[0]
    stopper = runner.split("function Stop-PortableAgent", 1)[1].split(
        "try {\n    if ($Scheduled", 1
    )[0]

    assert "Get-CimInstance" in process_helper
    assert "Get-WmiObject" in process_helper
    assert "Get-Process" in process_helper
    assert "Get-WindowsProcessInfo" in stopper
    assert "is not running; nothing needs to be stopped" in stopper


def test_portable_connection_check_is_short_safe_and_packaged():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    batch = (PORTABLE / "Check-Connection.bat").read_text(encoding="utf-8")
    builder = (
        ROOT / "deploy" / "windows" / "build-windows-portable.ps1"
    ).read_text(encoding="utf-8")
    checker = runner.split("function Test-PortableControllerConnection", 1)[1].split(
        "function Read-PlainSecret", 1
    )[0]

    assert '"https://controller.example.invalid"' in checker
    assert 'GetEnvironmentVariable("RIGPILOT_CONTROLLER_URL")' in checker
    assert 'Set RIGPILOT_CONTROLLER_URL' in checker
    assert "Resolve-DnsName" in checker
    assert "BeginConnect" in checker
    assert "WaitOne(3000" in checker
    assert '"/api/health"' in checker
    assert "-TimeoutSec 6" in checker
    assert "-UseBasicParsing" in checker
    assert "ServerCertificateValidationCallback" not in checker
    assert "SkipCertificateCheck" not in checker
    assert "RIGPILOT_ENROLLMENT_TOKEN" not in checker
    assert '"%~dp0portable-entry.ps1" -Action CheckConnection' in batch
    assert '"Check-Connection.bat"' in builder


def test_portable_update_uses_protected_version_slots_and_fixed_rollback_updater():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    updater = (PORTABLE / "RigPilotAgentUpdater.ps1").read_text(encoding="utf-8")
    builder = (
        ROOT / "deploy" / "windows" / "build-windows-portable.ps1"
    ).read_text(encoding="utf-8")

    assert "%ProgramData%" not in runner  # use the resolved environment path, not text expansion
    assert '"RigPilotAgentPortable"' in runner
    assert "Protect-PortableUpdateRoot" in runner
    assert '"/setowner", "*S-1-5-32-544"' in runner
    assert "New-PortableBoundaryAcl $true" in runner
    assert "New-PortableBoundaryAcl $false" in runner
    assert 'Set-Acl -LiteralPath $PortableRoot' in runner
    assert 'Set-Acl -LiteralPath $DataDir' in runner
    assert '$PortableRoot, "/inheritance:e", "/T"' not in runner
    assert '$PortableRoot, "/reset", "/T"' not in runner
    assert '$DataDir, "/inheritance:e", "/T"' not in runner
    assert '$DataDir, "/reset", "/T"' not in runner
    assert '$TrustedFile, "/inheritance:d"' in runner
    assert "AppendData" in runner
    assert "DeleteSubdirectoriesAndFiles" in runner
    assert "FileSystemRights]::Modify" not in runner
    protection = runner.split("function Protect-PortableUpdateRoot", 1)[1].split(
        "function Install-PortableAutostart", 1
    )[0]
    assert "agent-update-ready.json" in protection
    assert "Remove-Item -LiteralPath $ReadyPath" in protection
    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]
    assert installer.index("Test-OwnedAutostartTask $RegisteredTask") < installer.index(
        "Write-PortableUpdateReadyMarker"
    )
    assert installer.index("Start-ScheduledTask -TaskName $AutostartTaskName") < installer.index(
        "Write-PortableUpdateReadyMarker"
    )
    remover = runner.split("function Remove-PortableAutostart", 1)[1].split(
        "function Test-ControllerUrl", 1
    )[0]
    assert "active-agent.json" in runner
    assert 'versions/$Version/RigPilotAgent.exe' in runner
    assert '"RigPilotAgentUpdater.ps1"' in builder
    assert '"agent-release.json"' in builder
    assert "Get-FileHash" in builder
    assert "schema_version = 1" in builder
    assert "CommonApplicationData" in updater
    assert "$env:ProgramData" not in updater
    assert "Assert-PlainDirectoryChain $ProgramDataRoot" in updater
    assert "Wait-ProcessExit $AgentPid" in updater
    assert "updater-ready.json" in updater
    assert "updater_pid = $PID" in updater
    assert "Wait-ProcessExit $LauncherPid" in updater
    assert "[IO.Directory]::Move($IncomingSlot, $SlotPath)" in updater
    assert 'Install-StableFile "portable-agent.ps1"' not in updater
    assert "stable_launcher_retained = $true" in updater
    assert "AddSeconds(75)" in updater
    assert "health_heartbeats" in updater
    assert "rolled_back" in updater
    assert "Restore-FileAtomically" in updater
    assert "Stop-NewAgentIfOwned" in updater
    assert "Invoke-Expression" not in updater
    assert "Start-Process -FilePath $PowerShellPath" in updater



def test_portable_batch_entrypoints_stay_in_the_extracted_directory():
    entry = (PORTABLE / "portable-entry.ps1").read_text(encoding="utf-8")
    start = (PORTABLE / "Start-RigPilotAgent.bat").read_text(encoding="utf-8")
    stop = (PORTABLE / "Stop-RigPilotAgent.bat").read_text(encoding="utf-8")
    install = (PORTABLE / "Install-Autostart.bat").read_text(encoding="utf-8")
    remove = (PORTABLE / "Remove-Autostart.bat").read_text(encoding="utf-8")
    check = (PORTABLE / "Check-Connection.bat").read_text(encoding="utf-8")

    assert '"%~dp0portable-entry.ps1" -Action Start' in start
    assert '"%~dp0portable-entry.ps1" -Action Stop' in stop
    assert 'call "%~dp0Start-RigPilotAgent.bat" --install-autostart' in install
    assert '"%~dp0portable-entry.ps1" -Action RemoveAutostart' in remove
    assert '"%~dp0portable-entry.ps1" -Action CheckConnection' in check
    for script in (start, stop, install, remove, check):
        assert "schtasks" not in script.lower()
        assert "runas" not in script.lower()
        assert "programdata" not in script.lower()
        assert "setlocal EnableExtensions DisableDelayedExpansion" in script
    for script in (start, stop, remove, check):
        assert r'"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"' in script

    assert '[ValidateSet("Start", "Stop", "InstallAutostart", "RemoveAutostart", "CheckConnection")]' in entry
    assert "[Environment]::SystemDirectory" in entry
    assert "-Verb RunAs" in entry
    assert "-Wait" in entry
    assert "-PassThru" in entry
    assert 'Assert-PlainEntryPath $EntryPath "Portable compatibility entry" $false' in entry
    assert 'Join-Path $PortableRoot "data"' not in entry
    assert "active-agent.json" not in entry
    assert "Get-Content" not in entry
    assert "NativeErrorCode -eq 1223" in entry
    assert 'Join-Path $PortableRoot "portable-agent.ps1"' in entry
    assert "-Action $Action" in entry


@pytest.mark.skipif(os.name != "nt", reason="Windows batch and PowerShell syntax test")
def test_portable_powershell_and_batch_entrypoints_parse_and_route_with_spaces(tmp_path):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    parser = tmp_path / "parse-powershell.ps1"
    parser.write_text(
        "param([string]$Path)\n"
        "$tokens = $null\n"
        "$errors = $null\n"
        "[void][Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)\n"
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }\n",
        encoding="utf-8",
    )
    for script in PORTABLE.glob("*.ps1"):
        parsed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(parser),
                "-Path",
                str(script),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert parsed.returncode == 0, parsed.stderr

    portable = tmp_path / "portable path & symbols"
    portable.mkdir()
    for batch in PORTABLE.glob("*.bat"):
        shutil.copy2(batch, portable / batch.name)
    (portable / "portable-entry.ps1").write_text(
        "param([ValidateSet('Start','Stop','InstallAutostart','RemoveAutostart','CheckConnection')]"
        "[string]$Action)\n"
        "[IO.File]::WriteAllText((Join-Path $PSScriptRoot ($Action + '.called')), 'ok')\n"
        "exit 0\n",
        encoding="utf-8",
    )
    cases = (
        ("Start-RigPilotAgent.bat", "", "Start.called"),
        ("Start-RigPilotAgent.bat", "--install-autostart", "InstallAutostart.called"),
        ("Install-Autostart.bat", "", "InstallAutostart.called"),
        ("Stop-RigPilotAgent.bat", "", "Stop.called"),
        ("Remove-Autostart.bat", "", "RemoveAutostart.called"),
        ("Check-Connection.bat", "", "CheckConnection.called"),
    )
    for batch_name, argument, marker_name in cases:
        marker = portable / marker_name
        marker.unlink(missing_ok=True)
        command = f'call "{batch_name}"'
        if argument:
            command += f" {argument}"
        completed = subprocess.run(
            command,
            cwd=portable,
            input="\n",
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            shell=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert marker.is_file(), completed.stdout + completed.stderr


def test_portable_builder_freezes_smoke_tests_and_zips_the_bundle():
    builder = (
        ROOT / "deploy" / "windows" / "build-windows-portable.ps1"
    ).read_text(encoding="utf-8")

    assert "-m PyInstaller" in builder
    assert "--onedir" in builder
    assert "smoke-test-frozen-agent.ps1" in builder
    assert '"windows-portable.$PID.$([Guid]::NewGuid().ToString(\'N\'))"' in builder
    assert "随机发布构建目录发生冲突，拒绝复用" in builder
    assert "Remove-Item -LiteralPath $ResolvedWork -Recurse -Force" not in builder
    assert '$BuildVenv = Join-Path $WorkRoot "venv"' in builder
    assert "$BootstrapPythonPath -m venv $BuildVenv" in builder
    assert 'Join-Path $ProjectRoot "requirements-dev.txt"' in builder
    assert "-m pip check" in builder
    assert builder.index("-m pip check") < builder.index("-m pytest -q")
    assert "--basetemp $PytestBaseTemp" in builder
    assert '$PytestBaseTemp = Join-Path $WorkRoot "pytest"' in builder
    assert "Compress-Archive -Path" not in builder
    assert ".CreateEntry(" in builder
    assert '.Replace(\'\\\', \'/\')' in builder
    assert '"RigPilotAgent-Portable/$Relative"' in builder
    assert "[ValidateSet(" not in builder.split("[string]$ReleaseVersion", 1)[0]
    assert "[string]$ReleaseVersion," in builder
    assert "if (-not $ReleaseVersion)" in builder
    assert '$ReleaseVersion -ne $CurrentSourceVersion -and $ReleaseVersion -ne "0.5.6"' in builder
    assert "拒绝把当前 Agent 源码伪装为历史版本" in builder
    assert '$ReleaseVersion -eq "0.5.6"' in builder
    assert 'Join-Path $ProjectRoot "outputs"' in builder
    assert '"RigPilot Agent $ReleaseVersion"' in builder
    assert "RigPilotAgent-$ReleaseVersion-Windows-x64-Portable.zip" in builder
    assert "RigPilotAgent-0.5.6-to-0.6.0-UpdateBootstrap-Windows-x64.zip" in builder
    assert "bootstrap-source" in builder
    assert 'Join-Path $ProjectRoot "server\\__init__.py"' in builder
    assert 'Join-Path $ProjectRoot "server\\miner_adapters"' in builder
    assert "BOOTSTRAP-ONLY.txt" in builder
    assert '"ThirdPartyNotices.txt"' in builder
    assert '"Install-Autostart.bat"' in builder
    assert '"Remove-Autostart.bat"' in builder
    assert '"portable-entry.ps1"' in builder
    assert '"Enable-Power-Actions.bat"' not in builder
    assert '"Check-Connection.bat"' in builder
    assert '"RigPilotAgentUpdater.ps1"' in builder
    assert "agent-release.json" in builder
    assert "$ExpectedArchiveFiles" in builder
    assert "$VerifyArchive.Entries" in builder
    assert "ComputeHash($EntryStream)" in builder
    assert "ZIP 回读 SHA-256 不匹配" in builder
    assert "$VerifiedArchiveFiles.Count -ne $ExpectedArchiveFiles.Count" in builder
    assert '$ZipHashPath = "$ZipPath.sha256"' in builder
    assert '$ZipStage = "$ZipPath.part.$PID"' in builder
    assert "拒绝覆盖已有 Windows 发布物" in builder
    assert 'Get-FileHash -LiteralPath $ZipStage' in builder
    assert "PortableSha256File = $ZipHashPath" in builder
    assert "Register-ScheduledTask" not in builder
    assert "ISCC" not in builder


@pytest.mark.parametrize("scenario", ["success", "archive-collision", "sidecar-collision"])
def test_windows_release_publication_preserves_existing_outputs(tmp_path, scenario):
    result = _run_source_harness(tmp_path, ROOT / "deploy/windows/build-windows-portable.ps1", scenario, r'''
$Flow = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.TryStatementAst] -and
    $Node.Body.Extent.Text -match '^\{\s*\$ArchiveStream ='
}, $true))[0]
if ($null -eq $Flow) { throw "Archive publication flow is missing" }
$Text = $Flow.Body.Extent.Text
$Start = $Text.IndexOf('$ZipHash =')
$End = $Text.IndexOf('$PortableBytes =', $Start)
$Publish = [ScriptBlock]::Create($Text.Substring($Start, $End - $Start))
$Cleanup = [ScriptBlock]::Create(($Flow.Finally.Statements | ForEach-Object { $_.Extent.Text }) -join "`n")
$ArchiveName = "release.zip"
$ZipPath = Join-Path $Workspace $ArchiveName
$ZipHashPath = "$ZipPath.sha256"
$ZipStage = "$ZipPath.part"
$ZipHashStage = "$ZipHashPath.part"
$ArchivePublished = $false
[IO.File]::WriteAllText($ZipStage, "new archive")
if ($Scenario -eq "archive-collision") { [IO.File]::WriteAllText($ZipPath, "existing output") }
if ($Scenario -eq "sidecar-collision") { [IO.File]::WriteAllText($ZipHashPath, "existing output") }
$Failed = $false
try { . $Publish } catch { $Failed = $true } finally { . $Cleanup }
[pscustomobject]@{
    failed=$Failed
    archive=$(if ([IO.File]::Exists($ZipPath)) { [IO.File]::ReadAllText($ZipPath) } else { $null })
    sidecar=$(if ([IO.File]::Exists($ZipHashPath)) { [IO.File]::ReadAllText($ZipHashPath) } else { $null })
    stages_remain=([IO.File]::Exists($ZipStage) -or [IO.File]::Exists($ZipHashStage))
} | ConvertTo-Json -Compress
''')
    assert result["failed"] is (scenario != "success")
    assert result["archive"] == ("existing output" if scenario == "archive-collision" else "new archive")
    assert not result["stages_remain"]
    if scenario == "archive-collision":
        assert result["sidecar"] is None
    elif scenario == "sidecar-collision":
        assert result["sidecar"] == "existing output"
    else:
        assert result["sidecar"].endswith("  release.zip\n")


def test_windows_release_gate_covers_shared_agent_and_inheritance_contracts():
    builder = (ROOT / "deploy/windows/build-windows-portable.ps1").read_text(
        encoding="utf-8"
    )
    smoke = (ROOT / "deploy/windows/smoke-test-frozen-agent.ps1").read_text(
        encoding="utf-8"
    )
    assert r'Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "agent\tests")' in builder
    assert "test_hiveos_|test_linux_" in builder  # historical prefix filter stays harmless
    assert 'agent/tests/test_miner_packages.py' in builder
    assert 'agent/tests/test_pending_replacement.py' in builder
    for name in (
        "test_agent_enrollment_recovery.py",
        "test_device_replacement_and_settlement.py",
        "test_windows_installer.py",
        "test_windows_portable.py",
        "test_windows_portable_enrollment.py",
    ):
        assert f"tests/{name}" in builder
    assert 'Windows Agent release test files' in builder
    assert '--basetemp $PytestBaseTemp @SelectedTests' in builder
    assert 'SkipTests' not in builder.split('if (-not $SkipTests) {', 1)[1].split('$DistRoot', 1)[0]

    assert '/api/agents/$OriginalAgentId/replacement-token' in smoke
    assert 'RIGPILOT_REPLACEMENT_TOKEN' in smoke
    assert 'Invoke-FrozenAgentOnce -RunName "first-inheritance"' in smoke
    assert '$InheritedState.agent_id -ne $OriginalAgentId' in smoke
    assert '$InheritedState.agent_secret -eq $OldState.agent_secret' in smoke
    assert '$InheritedAgents.Count -ne 1' in smoke
    assert 'smoke-inheritance-retry.py' in smoke
    assert 'InheritanceRetry = "passed"' in smoke
    assert '$Retry.different_code_rejected_before_post -ne $true' in smoke
    assert (ROOT / "deploy/windows/smoke-inheritance-retry.py").is_file()


def test_portable_builder_pins_and_packages_cpu_temperature_helper():
    windows = ROOT / "deploy" / "windows"
    helper = windows / "cpu-temperature-helper"
    project = (helper / "RigPilot.CpuTemperature.csproj").read_text(
        encoding="utf-8"
    )
    builder = (windows / "build-windows-portable.ps1").read_text(
        encoding="utf-8"
    )
    lock = json.loads((helper / "packages.lock.json").read_text(encoding="utf-8"))
    notices = (helper / "THIRD-PARTY-NOTICES.txt").read_text(encoding="utf-8")

    assert "<TargetFramework>net472</TargetFramework>" in project
    assert "<RuntimeIdentifier>win-x64</RuntimeIdentifier>" in project
    assert "<SelfContained>false</SelfContained>" in project
    assert '<PackageReference Include="LibreHardwareMonitorLib" Version="0.9.6" />' in project
    assert '<PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies" Version="1.0.3" PrivateAssets="all" />' in project

    locked_targets = lock["dependencies"]
    assert locked_targets
    for dependencies in locked_targets.values():
        package = dependencies["LibreHardwareMonitorLib"]
        assert package["type"] == "Direct"
        assert package["requested"] == "[0.9.6, )"
        assert package["resolved"] == "0.9.6"
        assert package["contentHash"]
    framework_packages = [
        dependencies["Microsoft.NETFramework.ReferenceAssemblies"]
        for dependencies in locked_targets.values()
        if "Microsoft.NETFramework.ReferenceAssemblies" in dependencies
    ]
    assert len(framework_packages) == 1
    framework = framework_packages[0]
    assert framework["type"] == "Direct"
    assert framework["requested"] == "[1.0.3, )"
    assert framework["resolved"] == "1.0.3"
    assert framework["contentHash"]

    assert "LibreHardwareMonitorLib 0.9.6" in notices
    assert "https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6" in notices
    assert 'Get-Command dotnet.exe -ErrorAction SilentlyContinue' in builder
    assert '--list-sdks 2>$null' in builder
    assert "$DotNetVersionOutput.Count -gt 0" in builder
    assert "$DotNetVersionExitCode -ne 0" in builder
    assert "$DotNetPath restore $CpuTemperatureProject --locked-mode" in builder
    assert "$DotNetPath publish $CpuTemperatureProject" in builder
    assert "-r win-x64" in builder
    assert "--self-contained false" in builder
    assert '"RigPilot CPU Temperature Helper 1.0.0 (LibreHardwareMonitor 0.9.6)"' in builder
    assert 'Join-Path $BundleDir "tools\\cpu-temperature"' in builder
    assert "Get-ChildItem -LiteralPath $CpuTemperaturePublish -File" in builder
    assert '$CpuTemperatureItem.Extension -notin @(".pdb", ".xml")' in builder
    assert '"RigPilot.CpuTemperature.exe"' in builder
    assert '"THIRD-PARTY-NOTICES.txt"' in builder
    assert '"packages.lock.json"' in builder
    assert 'cpu-temperature-helper\\LICENSES' in builder
    assert 'Join-Path $CpuTemperatureBundle "LICENSES"' in builder
    license_files = {path.name for path in (helper / "LICENSES").iterdir()}
    assert {
        "MPL-2.0-LibreHardwareMonitor.txt",
        "LibreHardwareMonitor-THIRD-PARTY-NOTICES.txt",
        "Apache-2.0-HidSharp.txt",
        "MIT-dotnet.txt",
    } <= license_files


def test_frozen_agent_smoke_test_normalizes_duplicate_windows_path_keys():
    smoke_test = (
        ROOT / "deploy" / "windows" / "smoke-test-frozen-agent.ps1"
    ).read_text(encoding="utf-8")

    assert 'GetEnvironmentVariables("Process")' in smoke_test
    assert '[string]$_ -ieq "Path"' in smoke_test
    assert 'Remove-Item -LiteralPath "Env:$PathName"' in smoke_test
    assert 'ProcessorNameString' in smoke_test
    assert '$Agents[0].inventory.processor' in smoke_test
    # The selected source supplies the default; an explicit override is kept.
    # Every first contact and retry must compare against that same expectation.
    assert '$PSBoundParameters.ContainsKey("ExpectedVersion")' in smoke_test
    assert '$Agents[0].agent_version -ne $ExpectedVersion' in smoke_test
    assert '$InheritedAgents[0].agent_version -ne $ExpectedVersion' in smoke_test
    assert '--expected-version $ExpectedVersion' in smoke_test
    assert '$Agents[0].inventory.network' in smoke_test
    assert '$NetworkInventory.local_ip_addresses' in smoke_test
    assert '$NetworkInventory.public_ip_addresses' in smoke_test
    assert '$Agents[0].local_ip_addresses' in smoke_test


@pytest.mark.parametrize("scenario", ["exited", "timeout"])
def test_frozen_smoke_timeout_retains_evidence_and_uses_original_process_object(tmp_path, scenario):
    result = _run_source_harness(tmp_path, ROOT / "deploy/windows/smoke-test-frozen-agent.ps1", scenario, r'''
$Function = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $Node.Name -eq "Invoke-FrozenAgentOnce"
}, $true))[0]
. ([ScriptBlock]::Create($Function.Extent.Text))
$SmokeRoot = Join-Path $Workspace "owned-smoke"
[void][IO.Directory]::CreateDirectory($SmokeRoot)
$PreserveSmokeRoot = $false
$ConfigPath = Join-Path $Workspace "config.json"
$AgentExePath = Join-Path $Workspace "synthetic-agent.exe"
$script:Waits = @()
$script:Stopped = @()
function Start-Process {
    $Child = [pscustomobject]@{Id=12345; ExitCode=0; Handle=[IntPtr]12345}
    $Child | Add-Member ScriptMethod WaitForExit { param($Milliseconds)
        $script:Waits += $Milliseconds
        return $Scenario -eq "exited"
    }
    $Child | Add-Member ScriptMethod Refresh { }
    $Child | Add-Member ScriptMethod Kill { $script:Stopped += "original-process-object" }
    return $Child
}
$Failed = $false
try { Invoke-FrozenAgentOnce -RunName "synthetic" } catch { $Failed = $true }
$Cleanup = @($Ast.FindAll({ param($Node)
    $Node -is [Management.Automation.Language.IfStatementAst] -and
    $Node.Clauses[0].Item1.Extent.Text -eq '$PreserveSmokeRoot'
}, $true))
if ($Cleanup.Count -ne 1) { throw "Smoke directory cleanup guard is missing" }
. ([ScriptBlock]::Create($Cleanup[0].Extent.Text))
[pscustomobject]@{
    failed=$Failed; waits=@($script:Waits); stopped=@($script:Stopped)
    evidence_retained=[IO.Directory]::Exists($SmokeRoot)
} | ConvertTo-Json -Compress
''')
    assert result["waits"] == ([60000, 5000] if scenario == "timeout" else [60000])
    assert result["failed"] is (scenario == "timeout")
    assert result["stopped"] == (["original-process-object"] if scenario == "timeout" else [])
    assert result["evidence_retained"] is (scenario == "timeout")


def test_portable_readme_version_matches_agent_release():
    readme = (PORTABLE / "README-Portable.txt").read_text(encoding="utf-8")

    assert readme.startswith(f"RigPilot Agent {AGENT_VERSION} Windows 便携版\n")


def test_explicit_first_contact_codes_fail_before_uac_relaunch():
    entry = (PORTABLE / "portable-entry.ps1").read_text(encoding="utf-8")
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    entry_elevation = entry.split("if (-not (Test-IsAdministrator))", 1)[1].split(
        "& $PowerShellPath", 1
    )[0]
    runner_elevation = runner.split("if (-not (Test-IsAdministrator))", 1)[1].split(
        'if ($Action -eq "InstallAutostart")', 1
    )[0]
    for script in (entry_elevation, runner_elevation):
        guard = script.index("Explicit first-contact codes require an already elevated PowerShell session")
        assert "RIGPILOT_ENROLLMENT_TOKEN" in script[:guard]
        assert "RIGPILOT_REPLACEMENT_TOKEN" in script[:guard]
        assert guard < script.index("-Verb RunAs") if "-Verb RunAs" in script else guard < script.index("Restart-Elevated")
        assert "-ReplacementToken" not in script and "-EnrollmentToken" not in script


def test_first_contact_source_data_acl_is_locked_before_any_claim():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    first_contact = runner.split("function Initialize-PortableEnrollment", 1)[1].split(
        "function Stop-PortableAgent", 1
    )[0]
    assert first_contact.index("Assert-PortableFirstContactTargetAvailable") < first_contact.index(
        "Protect-PortableFirstContactData"
    )
    assert first_contact.index("Protect-PortableFirstContactData") < first_contact.index(
        "Write-PortableConfig"
    )
    assert first_contact.index("Assert-PortableFirstContactDataPrivate") < first_contact.index(
        "Invoke-EnrollmentRecovery"
    )
    assert first_contact.index("Assert-PortableFirstContactDataPrivate") < first_contact.index(
        "Invoke-InheritanceRecovery"
    )
    acl = runner.split("function Protect-PortableFirstContactData", 1)[1].split(
        "function Protect-PortableUpdateRoot", 1
    )[0]
    assert 'DriveFormat -cne "NTFS"' in acl
    assert "DriveType -ne [IO.DriveType]::Fixed" in acl
    assert "SourcePath.StartsWith(" in acl and "UNC or remote source path" in acl
    assert "Assert-PortableSourceParentChain" in acl
    assert "New-PortableBoundaryAcl $true" in acl
    assert "New-PortableBoundaryAcl $false" in acl
    assert "Set-Acl -LiteralPath $PortableRoot" in acl
    assert "Repair-PortableAclChildren $DataDir" in acl
    assert "Assert-PortablePrivateAcl" in acl
    attempt = runner.split("function Invoke-PortableEnrollmentAttempt", 1)[1].split(
        "function Invoke-Enrollment", 1
    )[0]
    assert attempt.index("& $AgentExe --config $ConfigPath --once") < attempt.index(
        "Protect-PortableFirstContactData", attempt.index("finally")
    )


def test_first_contact_parent_acl_does_not_trust_arbitrary_service_sids():
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    parent_gate = runner.split("function Assert-PortableSourceParentChain", 1)[1].split(
        "function Protect-PortableFirstContactData", 1
    )[0]
    assert '$TrustedSids = @("S-1-5-18", "S-1-5-32-544", $CurrentUserSid)' in parent_gate
    # The Windows Modules Installer SID is allowed only on an ACL-protected
    # volume root; arbitrary service SIDs remain outside the trusted set.
    assert (
        '$TrustedInstallerSid = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"'
        in parent_gate
    )
    assert "$Acl.AreAccessRulesProtected" in parent_gate
    assert "$VolumeRoot.TrimEnd(" in parent_gate
    assert "$OwnerSid -notin $TrustedSids -and -not $ProtectedInstallerVolumeRoot" in parent_gate
    assert "DeleteSubdirectoriesAndFiles" in parent_gate
    assert "$OwnerSid -notin $TrustedSids" in parent_gate
    assert "$Sid -notin $TrustedSids" in parent_gate


@pytest.mark.parametrize("scenario", ["late", "none"])
def test_updater_post_deadline_recheck_confirms_late_confirmation(tmp_path, scenario):
    """P2 (user-approved 2026-11-03): a confirmation written during the final
    sleep window of the activation loop must not be rolled back off-by-one."""
    result = _run_source_harness(tmp_path, PORTABLE / "RigPilotAgentUpdater.ps1", scenario, r'''
$Functions = @("Write-AtomicJson", "Test-ConfirmationReceipt", "Complete-ConfirmedActivation")
$FunctionTexts = @()
foreach ($Name in $Functions) {
    $Found = @($Ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq $Name
    })
    if ($Found.Count -ne 1) { throw "function not found: $Name" }
    $FunctionTexts += $Found[0].Extent.Text
}
$Flow = @($Ast.EndBlock.Statements | Where-Object {
    $_ -is [Management.Automation.Language.TryStatementAst]
})[-1]
$Stmts = @($Flow.Body.Statements)
$Start = -1
for ($i = 0; $i -lt $Stmts.Count; $i++) {
    if ($Stmts[$i].Extent.Text -like '$NewLauncherProcess = Start-ActiveAgent*') { $Start = $i; break }
}
if ($Start -lt 0) { throw "activation wait region not found" }
$Slice = ($Stmts[$Start..($Stmts.Count - 1)] | ForEach-Object { $_.Extent.Text }) -join "`n"
$Slice = $Slice.Replace('exit 0', '$script:ExitCode = 0; return')
$script:TestNow = [DateTime]::UtcNow
$script:WaitCount = 0
$Slice = $Slice.Replace('[DateTime]::UtcNow', '$script:TestNow')
# Advance to the deadline at the first sleep. A receipt written here can
# only be observed by the real post-loop recheck, regardless of host load.
$Slice = $Slice.Replace(
    'Start-Sleep -Seconds 1',
    '$script:WaitCount++; if ($script:WriteLate) { [IO.File]::WriteAllText($ConfirmationPath, $script:LateConfirmationJson) }; $script:TestNow = $Deadline')
if ($Slice -notmatch 'WriteLate') { throw "sleep patch did not apply" }

$Version = "0.9.9"
$ActivationId = "0123456789abcdef0123456789abcdef"
$AgentId = "00112233-4455-4666-8777-8899aabbccdd"
$CommandId = "01234567-89ab-4cde-8f01-23456789abcd"
$DataDir = Join-Path $Workspace "data"
[void][IO.Directory]::CreateDirectory((Join-Path $DataDir "agent-updates\confirmations"))
$ConfirmationPath = Join-Path $DataDir ("agent-updates\confirmations\$ActivationId.json")
$StatusPath = Join-Path $Workspace "activation-result.json"
$script:LateConfirmationJson = (@{
    schema_version = 1; activation_id = $ActivationId; command_id = $CommandId
    version = $Version; agent_id = $AgentId; health_heartbeats = 2
} | ConvertTo-Json -Compress)
$script:WriteLate = ($Scenario -eq "late")
$script:StartCount = 0
$NewLauncherProcess = $null
$script:ExitCode = -1
$script:Caught = $null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Start-ActiveAgent { $script:StartCount++; return $null }
$Body = ((($FunctionTexts -join "`n") + "`n") + $Slice)
try { & ([ScriptBlock]::Create($Body)) } catch { $script:ExitCode = 1; $script:Caught = $_.Exception.Message }
[pscustomobject]@{
    exit_code = $script:ExitCode
    caught = $script:Caught
    wait_count = $script:WaitCount
    confirmed_file = (Test-Path -LiteralPath $ConfirmationPath -PathType Leaf)
    receipt = $(if (Test-Path -LiteralPath $StatusPath -PathType Leaf) {
        (Get-Content -LiteralPath $StatusPath -Raw | ConvertFrom-Json).status } else { $null })
} | ConvertTo-Json -Compress
''')
    assert result["wait_count"] == 1
    if scenario == "late":
        assert result["exit_code"] == 0
        assert result["confirmed_file"] is True
        assert result["receipt"] == "succeeded"
    else:
        assert result["exit_code"] == 1
        assert "reconnect" in result["caught"]
        assert result["confirmed_file"] is False
        assert result["receipt"] is None


@pytest.mark.parametrize(
    "scenario",
    ["fresh", "expired-rollback", "clock-reset", "confirmed-reported", "confirmed-wrong-global-path",
     "restart-capped", "restart-retries", "fail-closed-pointer", "no-previous"],
)
def test_updater_recover_bounded_semantics(tmp_path, scenario):
    """P3/P4 (user-approved 2026-11-03): the Windows updater's bounded
    anchor-level recovery, executed against a synthetic workspace layout."""
    result = _run_source_harness(tmp_path, PORTABLE / "RigPilotAgentUpdater.ps1", scenario, r'''
$Functions = @(
    "Write-AtomicJson", "Assert-PlainDirectory", "Restore-FileAtomically",
    "Test-ConfirmationReceipt", "Test-RecoveryDeadlineExpired",
    "Stop-NewAgentIfOwned", "Get-PlainJsonFile", "Remove-StagedPayload",
    "Test-PortableAgentRunning", "Complete-PendingRecoveryRestart",
    "Remove-AbandonedUpdateSlots",
    "Invoke-PortableRecovery"
)
$FunctionTexts = @()
foreach ($Name in $Functions) {
    $Found = @($Ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -eq $Name
    })
    if ($Found.Count -ne 1) { throw "function not found: $Name" }
    $FunctionTexts += $Found[0].Extent.Text
}

$Root = Join-Path $Workspace "root"
$DataDir = Join-Path $Root "data"
$Staged = Join-Path $DataDir "agent-updates\staged"
$ActivationId = "0123456789abcdef0123456789abcdef"
$AgentId = "00112233-4455-4666-8777-8899aabbccdd"
$CommandId = "01234567-89ab-4cde-8f01-23456789abcd"
$NewVersion = "0.9.9"
$OldVersion = "0.9.8"
$Job = Join-Path $Staged $ActivationId
foreach ($Dir in @(
    $Job, (Join-Path $DataDir "agent-updates\confirmations"),
    (Join-Path $DataDir "agent-updates\reported"),
    (Join-Path $Root "versions\$NewVersion"), (Join-Path $Root "versions\$OldVersion")
)) { [void][IO.Directory]::CreateDirectory($Dir) }
[IO.File]::WriteAllText((Join-Path $Root "versions\$NewVersion\RigPilotAgent.exe"), "new")
[IO.File]::WriteAllText((Join-Path $Root "versions\$OldVersion\RigPilotAgent.exe"), "old")
$PointerPath = Join-Path $DataDir "active-agent.json"
$NewPointer = @{
    schema_version = 1; version = $NewVersion
    relative_executable = "versions/$NewVersion/RigPilotAgent.exe"
    activation_id = $ActivationId; command_id = $CommandId
} | ConvertTo-Json -Compress
$OldPointer = @{
    schema_version = 1; version = $OldVersion
    relative_executable = "versions/$OldVersion/RigPilotAgent.exe"
    activation_id = "fedcba9876543210fedcba9876543210"
    command_id = "fedcba98-7654-4321-8210-fedcba987654"
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText($PointerPath, $NewPointer)
[IO.File]::WriteAllText((Join-Path $Job "active-agent.previous.json"), $OldPointer)
[IO.File]::WriteAllText((Join-Path $Job "updater-ready.json"), (@{
    schema_version = 1; activation_id = $ActivationId; command_id = $CommandId
    agent_id = $AgentId; updater_pid = 4242 } | ConvertTo-Json -Compress))
[IO.File]::WriteAllText((Join-Path $Job "release.zip"), "package-bytes")
$ConfirmationPath = Join-Path $DataDir ("agent-updates\confirmations\$ActivationId.json")

if ($Scenario -eq "clock-reset") {
    [IO.File]::SetLastWriteTimeUtc($PointerPath, [DateTime]::UtcNow.AddHours(1))
}
elseif ($Scenario -eq "fresh") {
    [IO.File]::SetLastWriteTimeUtc($PointerPath, [DateTime]::UtcNow)
}
else {
    [IO.File]::SetLastWriteTimeUtc($PointerPath, [DateTime]::UtcNow.AddSeconds(-400))
}

if ($Scenario -in @("confirmed-reported", "confirmed-wrong-global-path")) {
    [IO.File]::WriteAllText($ConfirmationPath, (@{
        schema_version = 1; activation_id = $ActivationId; command_id = $CommandId
        version = $NewVersion; agent_id = $AgentId; health_heartbeats = 2
    } | ConvertTo-Json -Compress))
    [IO.File]::WriteAllText((Join-Path $DataDir ("agent-updates\reported\$ActivationId.json")), (@{
        schema_version = 1; activation_id = $ActivationId; command_id = $CommandId
        version = $NewVersion; agent_id = $AgentId; reported = $true
    } | ConvertTo-Json -Compress))
}
if ($Scenario -eq "confirmed-wrong-global-path") {
    # Actual scheduled recovery receives no ActivationId. A stale/empty global
    # must not cause a valid pointer-selected confirmation to be overlooked.
    $ConfirmationPath = Join-Path $DataDir "agent-updates\confirmations\.json"
}
if ($Scenario -eq "fail-closed-pointer") {
    [IO.File]::WriteAllText($PointerPath, (@{
        schema_version = 1; version = $NewVersion
        relative_executable = "versions/$NewVersion/RigPilotAgent.exe"
        activation_id = $ActivationId; command_id = $CommandId; extra = "field"
    } | ConvertTo-Json -Compress))
    [IO.File]::SetLastWriteTimeUtc($PointerPath, [DateTime]::UtcNow.AddSeconds(-400))
}
if ($Scenario -eq "no-previous") {
    Remove-Item -LiteralPath (Join-Path $Job "active-agent.previous.json") -Force
}
if ($Scenario -in @("restart-capped", "restart-retries")) {
    # The rollback already restored the old pointer; only the bounded
    # launcher restart remains pending.
    [IO.File]::WriteAllText($PointerPath, $OldPointer)
    $Attempts = 5
    if ($Scenario -eq "restart-retries") { $Attempts = 1 }
    [IO.File]::WriteAllText((Join-Path $Job "recovery-restart.json"), (@{
        schema_version = 1; activation_id = $ActivationId; version = $NewVersion
        restore_version = $OldVersion
        restore_relative = "versions/$OldVersion/RigPilotAgent.exe"
        attempts = $Attempts } | ConvertTo-Json -Compress))
}

# Script-scope variables the extracted anchor functions reference.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$PointerPath = $PointerPath
$DataRoot = $DataDir
$SlotPath = Join-Path $Root ("versions\$NewVersion")
$PowerShellPath = "powershell.exe"
$LauncherPath = Join-Path $Root "portable-agent.ps1"
$RecoveryGraceSeconds = 195
$RecoveryClockResetToleranceTicks = 50000000L
$RecoveryRestartAttemptLimit = 5
$script:StartCount = 0
function Start-ActiveAgent { $script:StartCount++; return $null }
$script:RecoverExit = $null
try {
    $script:RecoverExit = & ([ScriptBlock]::Create(((($FunctionTexts -join "`n") + "`n") +
        'Invoke-PortableRecovery -Root $Root')))
}
catch { $script:RecoverExit = "threw: " + $_.Exception.Message }
$MarkerPath = Join-Path $Job "recovery-restart.json"
[pscustomobject]@{
    exit = $script:RecoverExit
    pointer_version = ((Get-Content -LiteralPath $PointerPath -Raw | ConvertFrom-Json).version)
    receipt_status = $(if (Test-Path (Join-Path $Job "activation-result.json")) {
        (Get-Content -LiteralPath (Join-Path $Job "activation-result.json") -Raw | ConvertFrom-Json).status } else { $null })
    receipt_agent_id = $(if (Test-Path (Join-Path $Job "activation-result.json")) {
        [string]((Get-Content -LiteralPath (Join-Path $Job "activation-result.json") -Raw | ConvertFrom-Json).agent_id) } else { $null })
    marker_attempts = $(if (Test-Path -LiteralPath $MarkerPath) {
        [int]((Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json).attempts) } else { $null })
    marker_exists = (Test-Path -LiteralPath $MarkerPath)
    quarantined = (Test-Path (Join-Path $DataDir ("agent-updates\quarantine\$NewVersion-$ActivationId")))
    release_zip_exists = (Test-Path -LiteralPath (Join-Path $Job "release.zip"))
    previous_kept = (Test-Path -LiteralPath (Join-Path $Job "active-agent.previous.json"))
    started = $script:StartCount
} | ConvertTo-Json -Compress
''')
    if scenario == "fresh":
        assert result["exit"] == 0
        assert result["pointer_version"] == "0.9.9"
        assert result["receipt_status"] is None
        assert result["marker_exists"] is False
        assert result["started"] == 0
        assert result["release_zip_exists"] is True
    elif scenario in {"expired-rollback", "clock-reset"}:
        assert result["exit"] == 1
        assert result["pointer_version"] == "0.9.8"
        assert result["receipt_status"] == "rolled_back"
        assert result["receipt_agent_id"] == "00112233-4455-4666-8777-8899aabbccdd"
        assert result["marker_attempts"] == 0
        assert result["quarantined"] is True
        assert result["started"] == 1
        assert result["release_zip_exists"] is True
    elif scenario in {"confirmed-reported", "confirmed-wrong-global-path"}:
        assert result["exit"] == 0
        assert result["pointer_version"] == "0.9.9"
        assert result["receipt_status"] == "succeeded"
        assert result["release_zip_exists"] is False
        assert result["previous_kept"] is False
        assert result["started"] == 0
    elif scenario == "restart-capped":
        assert result["exit"] == 0
        assert result["started"] == 0
        assert result["marker_attempts"] == 5
    elif scenario == "restart-retries":
        assert result["exit"] == 0
        assert result["started"] == 1
        assert result["marker_attempts"] == 2
    elif scenario == "fail-closed-pointer":
        assert result["exit"] == 0
        assert result["pointer_version"] == "0.9.9"
        assert result["receipt_status"] is None
        assert result["quarantined"] is False
        assert result["started"] == 0
    else:  # no-previous
        assert result["exit"] == 0
        assert result["pointer_version"] == "0.9.9"
        assert result["receipt_status"] is None
        assert result["started"] == 0


def test_windows_update_recovery_watchdog_wiring():
    """P3 structural wiring: launcher installs/removes the recovery task, the
    task invokes the TRUSTED root updater in bounded recover mode, and the
    updater arms the watchdog before taking activation ownership."""
    runner = (PORTABLE / "portable-agent.ps1").read_text(encoding="utf-8")
    updater = (PORTABLE / "RigPilotAgentUpdater.ps1").read_text(encoding="utf-8")

    assert "RigPilotAgentUpdateRecovery-" in runner
    assert "RigPilotAgentUpdateRecovery-" in updater
    installer = runner.split("function Install-PortableAutostart", 1)[1].split(
        "function Remove-PortableAutostart", 1
    )[0]
    assert installer.index("Install-PortableRecoveryWatchdog") < installer.index(
        "Start-ScheduledTask -TaskName $AutostartTaskName"
    )
    assert installer.index("Install-PortableRecoveryWatchdog") < installer.index(
        "Write-PortableUpdateReadyMarker"
    )
    remover = runner.split("function Remove-PortableAutostart", 1)[1].split(
        "function Test-ControllerUrl", 1
    )[0]
    assert "Remove-PortableRecoveryWatchdog" in remover

    # The recovery task must run the trusted root updater, never a staged
    # copy, in bounded recover mode with the fixed root.
    assert '-Action Recover -PortableRoot "' in updater
    assert "Invoke-PortableRecovery" in updater
    assert "RecoveryRestartAttemptLimit" in updater
    assert "50000000L" in updater  # 5s no-RTC clock-reset tolerance in file ticks
    # Arming happens inside the activate flow after manifest verification and
    # before the readiness marker claims ownership of the activation, and is
    # best-effort so it can never fail a healthy activation.
    prelude = updater.split("Write-AtomicJson $ExpectedReadyPath", 1)[0]
    assert prelude.rindex("\n    Assert-StagedManifest") < prelude.rindex(
        "\n        Install-PortableRecoveryWatchdog"
    )
    watchdog_call = prelude.split("function Install-PortableRecoveryWatchdog", 1)[1]
    assert "Write-Warning" in watchdog_call
    # Recovery failure is bounded, never silent success.
    recover_block = updater.split('if ($Action -eq "Recover")', 1)[1]
    assert "exit (Invoke-PortableRecovery" in recover_block
