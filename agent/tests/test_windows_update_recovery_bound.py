"""Run the installed Windows updater's per-transaction restart decisions against private files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    "absent", "recent-handoff", "flat-after-handoff", "flat-from-candidate", "slot-from-candidate",
    "original-running", "verified-old-running", "attempt-bound", "changed-agent", "rotated-secret",
    "changed-executable", "superseded-pointer", "confirmed-candidate", "operator-cancel",
    "delayed-launch-after-cancel", "candidate-launch", "candidate-launch-after-stop",
    "superseded-before-delayed-launch", "expired-handoff-old-survived", "large-settled-history",
)
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell updater behavior")


@pytest.fixture(scope="module")
def outcomes(tmp_path_factory):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    workspace = tmp_path_factory.mktemp("bound-recovery")
    script = ROOT / "tests/fixtures/windows_update_recovery_bound.ps1"
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-Source", str(ROOT/"deploy/windows/portable"), "-Workspace", str(workspace)],
        cwd=workspace, capture_output=True, encoding="utf-8", errors="replace", timeout=90,
    )
    assert result.returncode == 0, result.stderr[-3500:]
    outcomes = json.loads(result.stdout.lstrip("\ufeff"))
    assert set(outcomes) == set(CASES)
    return outcomes


@pytest.mark.parametrize("case", CASES)
def test_recovery_intent_decision(outcomes, case):
    assert outcomes[case] == "passed", outcomes[case]
