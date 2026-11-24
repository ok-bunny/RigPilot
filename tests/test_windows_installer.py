from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_obsolete_windows_installer_sources_are_removed():
    windows = ROOT / "deploy" / "windows"
    assert not (windows / "build-windows-installer.ps1").exists()
    assert not (windows / "RigPilotAgent.iss").exists()
    assert (windows / "build-windows-portable.ps1").is_file()


def test_task_migration_accepts_owned_legacy_tasks_and_has_fallback():
    uninstall_script = (ROOT / "deploy" / "uninstall-agent-task.ps1").read_text(
        encoding="utf-8"
    )
    install_script = (ROOT / "deploy" / "install-agent-task.ps1").read_text(
        encoding="utf-8"
    )

    assert '$TaskPath = "\\"' in uninstall_script
    assert "$ExpectedConfigPath" in uninstall_script
    assert "$ExpectedWorkingDirectory" in uninstall_script
    assert "$IsExpectedTask" in uninstall_script
    assert "schtasks.exe" in uninstall_script
    assert "Unregister-ScheduledTask" in uninstall_script

    assert '$TaskPath = "\\"' in install_script
    assert "$IsReplaceableRigPilotTask" in install_script
    assert "Register-ScheduledTask" in install_script
    assert "-Force | Out-Null" in install_script
    assert '-UserId "SYSTEM"' in install_script
    assert '-RunLevel Highest' in install_script


def test_task_migration_keeps_same_name_task_ownership_guard():
    uninstall_script = (ROOT / "deploy" / "uninstall-agent-task.ps1").read_text(
        encoding="utf-8"
    )
    install_script = (ROOT / "deploy" / "install-agent-task.ps1").read_text(
        encoding="utf-8"
    )

    assert "$HasKnownDescription" in uninstall_script
    assert "$AllowLegacyEmptyDescription" in uninstall_script
    assert "multiple actions" in uninstall_script
    assert "$HasKnownDescription" in install_script
    assert "multiple actions" in install_script
