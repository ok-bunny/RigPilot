from __future__ import annotations

import os
import sys

from agent.external_process import external_process_environment


def test_linux_external_environment_restores_pyinstaller_loader_path(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123:/usr/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("WATCHDOG_USEC", "120000000")
    monkeypatch.setenv("WATCHDOG_PID", "42")

    result = external_process_environment("Linux")

    assert result["LD_LIBRARY_PATH"] == "/usr/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in result
    assert "NOTIFY_SOCKET" not in result
    assert "WATCHDOG_USEC" not in result
    assert "WATCHDOG_PID" not in result
    assert os.environ["LD_LIBRARY_PATH"].startswith("/tmp/_MEI123")


def test_lowercase_linux_from_production_composition_is_also_sanitized(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI789:/usr/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")

    result = external_process_environment("linux")

    assert result["LD_LIBRARY_PATH"] == "/usr/lib"
    assert "NOTIFY_SOCKET" not in result

def test_linux_frozen_environment_without_saved_path_removes_loader_path(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI456")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEI456", raising=False)

    result = external_process_environment("Linux")

    assert "LD_LIBRARY_PATH" not in result


def test_linux_source_environment_preserves_user_python_and_library_settings(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/operator/lib")
    monkeypatch.setenv("PYTHONPATH", "/opt/operator/python-modules")
    monkeypatch.setenv("PYTHONHOME", "/opt/operator/python")
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    original_environment = dict(os.environ)

    result = external_process_environment("Linux")

    assert result["LD_LIBRARY_PATH"] == "/opt/operator/lib"
    assert result["PYTHONPATH"] == "/opt/operator/python-modules"
    assert result["PYTHONHOME"] == "/opt/operator/python"
    assert "NOTIFY_SOCKET" not in result
    assert dict(os.environ) == original_environment


def test_non_linux_environment_is_an_unmodified_copy(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "C:/private")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "C:/original")
    monkeypatch.setenv("NOTIFY_SOCKET", "unchanged")
    monkeypatch.setenv("PYTHONPATH", r"C:\operator\python-modules")
    monkeypatch.setenv("PYTHONHOME", r"C:\operator\python")
    original_environment = dict(os.environ)

    result = external_process_environment("Windows")

    assert result["LD_LIBRARY_PATH"] == "C:/private"
    assert result["LD_LIBRARY_PATH_ORIG"] == "C:/original"
    assert result["NOTIFY_SOCKET"] == "unchanged"
    assert result["PYTHONPATH"] == r"C:\operator\python-modules"
    assert result["PYTHONHOME"] == r"C:\operator\python"
    assert result == original_environment
    assert dict(os.environ) == original_environment
    assert result is not os.environ
