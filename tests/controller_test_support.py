"""Small, stateless factories shared by Controller API tests."""


from __future__ import annotations


import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: Any, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def create_user(client: Any, admin_token: str, username: str, role: str) -> str:
    password = f"long-enough-password-for-{username}"
    response = client.post(
        "/api/users",
        headers=auth(admin_token),
        json={"username": username, "password": password, "role": role},
    )
    assert response.status_code == 201, response.text
    return login(client, username, password)


def gpu_oc_success_result(command: dict[str, object]) -> dict[str, object]:
    payload = command["payload"]
    assert isinstance(payload, dict)
    settings = payload["settings"]
    assert isinstance(settings, list)
    if payload.get("mode") == "reset_one":
        return {
            "vendor": payload["vendor"],
            "gpu_profile_id": payload["gpu_profile_id"],
            "operations": [],
            "reset": [
                {
                    "index": setting["index"],
                    "reset": [key for key in setting if key != "index"],
                }
                for setting in settings
            ],
        }
    return {
        "vendor": payload["vendor"],
        "gpu_profile_id": payload["gpu_profile_id"],
        "operations": [
            {
                "index": setting["index"],
                "applied": [key for key in setting if key != "index"],
            }
            for setting in settings
        ],
        "reset": [],
    }


def portable_zip(
    *,
    executable: bytes = b"portable-executable",
    extra_entries: dict[str, bytes] | None = None,
    version: str = "0.6.0",
) -> bytes:
    files = {
        "RigPilotAgent.exe": executable,
        "RigPilotAgentUpdater.ps1": b"# trusted updater\r\n",
        "portable-agent.ps1": b"# portable supervisor\r\n",
        "Start-RigPilotAgent.bat": b"@echo off\r\npowershell -File portable-agent.ps1\r\n",
        **(extra_entries or {}),
    }
    manifest = {
        "schema_version": 1,
        "version": version,
        "platform": "windows",
        "architecture": "x86_64",
        "files": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in files.items()
        },
    }
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        root = "RigPilotAgent-Portable/"
        for name, payload in files.items():
            archive.writestr(root + name, payload)
        archive.writestr(root + "agent-release.json", json.dumps(manifest))
    return stream.getvalue()


def controller_settings_kwargs(tmp_path: Path) -> dict[str, object]:
    """Parameters used by the identical, function-scoped Controller fixtures."""
    return {
        "data_dir": tmp_path,
        "database_path": tmp_path / "test.db",
        "secret_key": "test-secret-key-that-is-long-and-random-enough",
        "admin_username": "admin",
        "admin_password": "correct-horse-battery-staple",
        "agent_online_seconds": 60,
    }
