import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_windows_entrypoint_preserves_venv_and_reports_missing_interpreter(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell execution is unavailable; no Windows runtime claim")
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    entrypoint = scripts / "run-server.ps1"
    entrypoint.write_text((ROOT / "scripts/run-server.ps1").read_text())
    venv = project / ".venv"
    venv.mkdir()
    marker = venv / "keep-existing-environment"
    marker.write_bytes(b"existing environment")
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(entrypoint)],
        cwd=tmp_path, env={"PATH": os.defpath}, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "no Windows Python interpreter" in result.stdout + result.stderr
    assert marker.read_bytes() == b"existing environment"
    assert not (venv / "Scripts/python.exe").exists()
    assert not (project / "data").exists()
