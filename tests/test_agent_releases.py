from __future__ import annotations

import asyncio
import io
import hashlib
import json
import sqlite3
import struct
import tarfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from controller_test_support import auth, login, portable_zip

from server.config import Settings
from server.main import (
    _agent_release_storage_path,
    _validate_linux_agent_tar_gz,
    create_app,
)


ADMIN_PASSWORD = "correct-horse-battery-staple"


def linux_update_tar(
    *,
    executable: bytes = b"linux-agent-executable",
    version: str = "0.6.6",
    extra_files: dict[str, bytes] | None = None,
    manifest_overrides: dict[str, object] | None = None,
    root: str = "RigPilotAgent-OnlineUpdate",
) -> bytes:
    files = {"RigPilotAgent": executable, **(extra_files or {})}
    manifest: dict[str, object] = {
        "schema_version": 1,
        "version": version,
        "platform": "linux",
        "architecture": "x86_64",
        "files": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in files.items()
        },
    }
    manifest.update(manifest_overrides or {})
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        root_entry = tarfile.TarInfo(root)
        root_entry.type = tarfile.DIRTYPE
        root_entry.mode = 0o755
        archive.addfile(root_entry)
        for name, payload in files.items():
            entry = tarfile.TarInfo(f"{root}/{name}")
            entry.size = len(payload)
            entry.mode = 0o755 if name == "RigPilotAgent" else 0o644
            archive.addfile(entry, io.BytesIO(payload))
        encoded_manifest = json.dumps(manifest, separators=(",", ":")).encode()
        entry = tarfile.TarInfo(f"{root}/agent-release.json")
        entry.size = len(encoded_manifest)
        entry.mode = 0o644
        archive.addfile(entry, io.BytesIO(encoded_manifest))
    return stream.getvalue()


@pytest.fixture()
def release_app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        max_agent_release_bytes=64 * 1024,
    )
    with TestClient(create_app(settings)) as client:
        yield client, settings


def upload(
    client: TestClient,
    token: str,
    payload: bytes,
    *,
    version: str = "0.6.0",
    file_name: str = "RigPilotAgent-0.6.0-Windows-x64-Portable.zip",
    platform: str = "windows",
    architecture: str = "x86_64",
):
    return client.post(
        "/api/agent-releases/upload",
        headers={
            **auth(token),
            "Content-Type": (
                "application/gzip" if platform == "linux" else "application/zip"
            ),
        },
        params={
            "version": version,
            "platform": platform,
            "architecture": architecture,
            "file_name": file_name,
            "notes": "tested portable release",
        },
        content=payload,
    )


def enroll_update_agent(
    client: TestClient,
    admin_token: str,
    *,
    name: str = "update-canary",
    os_family: str = "windows",
    version: str = "0.5.7",
    capabilities: list[str] | None = None,
    heartbeat: bool = True,
) -> tuple[str, str]:
    token_response = client.post(
        "/api/enrollment-tokens",
        headers=auth(admin_token),
        json={"label": name, "ttl_minutes": 30},
    )
    assert token_response.status_code == 200, token_response.text
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": token_response.json()["token"],
            "name": name,
            "hostname": f"{name}.local",
            "os_family": os_family,
            "platform": "Windows 10 x64" if os_family == "windows" else "Ubuntu 24.04",
            "agent_version": version,
        },
    )
    assert enrolled.status_code == 201, enrolled.text
    agent_id = enrolled.json()["agent_id"]
    secret = enrolled.json()["agent_secret"]
    if heartbeat:
        response = client.post(
            "/api/agent/heartbeat",
            headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
            json={
                "hostname": f"{name}.local",
                "os_family": os_family,
                "platform": "Windows 10 x64" if os_family == "windows" else "Ubuntu 24.04",
                "agent_version": version,
                "capabilities": capabilities or [],
                "inventory": {"architecture": "AMD64"},
            },
        )
        assert response.status_code == 200, response.text
    return agent_id, secret


def test_agent_release_repository_is_admin_only(release_app):
    client, settings = release_app
    admin_token = login(client, "admin", ADMIN_PASSWORD)
    for username, role in (("viewer-release", "viewer"), ("operator-release", "operator")):
        response = client.post(
            "/api/users",
            headers=auth(admin_token),
            json={"username": username, "password": ADMIN_PASSWORD, "role": role},
        )
        assert response.status_code == 201, response.text
    viewer_token = login(client, "viewer-release", ADMIN_PASSWORD)
    operator_token = login(client, "operator-release", ADMIN_PASSWORD)
    payload = portable_zip()

    forbidden = upload(client, operator_token, payload)
    assert forbidden.status_code == 403

    created = upload(client, admin_token, payload)
    assert created.status_code == 201, created.text
    release = created.json()
    assert release == {
        "id": release["id"],
        "version": "0.6.0",
        "platform": "windows",
        "architecture": "x86_64",
        "file_name": "RigPilotAgent-0.6.0-Windows-x64-Portable.zip",
        "size": len(payload),
        "sha256": release["sha256"],
        "update_protocol": "agent_update.v1",
        "deployable": True,
        "notes": "tested portable release",
        "created_by": "admin",
        "created_at": release["created_at"],
        "download_url": f"/api/agent-releases/{release['id']}/download",
    }
    assert len(release["sha256"]) == 64

    assert client.get("/api/agent-releases").status_code == 401
    assert client.get("/api/agent-releases", headers=auth(viewer_token)).status_code == 403
    assert client.get("/api/agent-releases", headers=auth(operator_token)).status_code == 403
    listed = client.get("/api/agent-releases", headers=auth(admin_token))
    assert listed.status_code == 200
    assert listed.json() == [release]

    assert client.get(release["download_url"]).status_code == 401
    assert client.get(release["download_url"], headers=auth(viewer_token)).status_code == 403
    assert client.get(release["download_url"], headers=auth(operator_token)).status_code == 403
    downloaded = client.get(release["download_url"], headers=auth(admin_token))
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["content-type"].startswith("application/zip")
    assert downloaded.headers["cache-control"] == "private, no-store"

    with sqlite3.connect(settings.database_path) as db:
        storage_key = db.execute(
            "SELECT storage_key FROM agent_releases WHERE id=?", (release["id"],)
        ).fetchone()[0]
    stored_path = _agent_release_storage_path(settings, storage_key)
    assert stored_path.is_file()

    assert (
        client.delete(
            f"/api/agent-releases/{release['id']}", headers=auth(operator_token)
        ).status_code
        == 403
    )
    deleted = client.delete(
        f"/api/agent-releases/{release['id']}", headers=auth(admin_token)
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted"}
    assert not stored_path.exists()
    assert client.get("/api/agent-releases", headers=auth(admin_token)).json() == []
    assert (
        client.get(release["download_url"], headers=auth(admin_token)).status_code
        == 404
    )
    audit = client.get("/api/audit", headers=auth(admin_token)).json()
    actions = {entry["action"] for entry in audit}
    assert {"agent_release_created", "agent_release_deleted"} <= actions


def test_release_deploy_expires_stale_unfinished_command_before_conflict_check(
    release_app,
):
    client, settings = release_app
    admin_token = login(client, "admin", ADMIN_PASSWORD)
    release_payload = portable_zip(version="0.5.7")
    release = upload(
        client,
        admin_token,
        release_payload,
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    ).json()
    agent_id, _ = enroll_update_agent(
        client,
        admin_token,
        name="stale-command-canary",
        version="0.5.6",
        capabilities=["agent_update.v1"],
    )
    stale = client.post(
        f"/api/agents/{agent_id}/commands",
        headers=auth(admin_token),
        json={"action": "collect_diagnostics", "payload": {}},
    )
    assert stale.status_code == 201, stale.text
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "UPDATE commands SET expires_at=? WHERE id=?",
            ("2000-01-01T00:00:00+00:00", stale.json()["id"]),
        )
        db.commit()

    deployed = client.post(
        f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(admin_token),
        json={"agent_id": agent_id},
    )
    assert deployed.status_code == 201, deployed.text
    with sqlite3.connect(settings.database_path) as db:
        status_value = db.execute(
            "SELECT status FROM commands WHERE id=?", (stale.json()["id"],)
        ).fetchone()[0]
    assert status_value == "expired"


def test_linux_online_release_upload_download_and_platform_matched_deploy(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    linux_payload = linux_update_tar(version="0.6.8")
    linux_created = upload(
        client,
        token,
        linux_payload,
        version="0.6.8",
        platform="linux",
        architecture="amd64",
        file_name="RigPilotAgent-0.6.8-Ubuntu-x86_64-OnlineUpdate.tar.gz",
    )
    assert linux_created.status_code == 201, linux_created.text
    linux_release = linux_created.json()
    assert linux_release["platform"] == "linux"
    assert linux_release["architecture"] == "x86_64"
    assert linux_release["update_protocol"] == "agent_update.v1"

    # A platform is part of the uniqueness key, so the same semantic version
    # can coexist safely for Windows and Linux.
    windows_created = upload(
        client,
        token,
        portable_zip(version="0.6.6"),
        version="0.6.6",
        file_name="RigPilotAgent-0.6.6-Windows-x64-Portable.zip",
    )
    assert windows_created.status_code == 201, windows_created.text
    assert len(client.get("/api/agent-releases", headers=auth(token)).json()) == 2

    downloaded = client.get(linux_release["download_url"], headers=auth(token))
    assert downloaded.status_code == 200
    assert downloaded.content == linux_payload
    assert downloaded.headers["content-type"].startswith("application/gzip")

    linux_id, _linux_secret = enroll_update_agent(
        client,
        token,
        name="linux-update-canary",
        os_family="linux",
        version="0.6.7",
        capabilities=["agent_update.v1"],
    )
    windows_id, _windows_secret = enroll_update_agent(
        client,
        token,
        name="windows-not-linux",
        version="0.6.5",
        capabilities=["agent_update.v1"],
    )
    deploy_url = f"/api/agent-releases/{linux_release['id']}/deploy"
    mismatch = client.post(
        deploy_url, headers=auth(token), json={"agent_id": windows_id}
    )
    assert mismatch.status_code == 409
    assert "Linux" in mismatch.json()["detail"]
    queued = client.post(
        deploy_url, headers=auth(token), json={"agent_id": linux_id}
    )
    assert queued.status_code == 201, queued.text
    assert queued.json()["target_version"] == "0.6.8"

    # Merely uploading never creates a command; only the explicit single
    # device deploy above does.
    with sqlite3.connect(settings.database_path) as db:
        commands = db.execute(
            "SELECT agent_id, action FROM commands ORDER BY created_at"
        ).fetchall()
    assert commands == [(linux_id, "agent_update.v1")]


def test_linux_066_online_update_requires_one_manual_repair_install(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    created = upload(
        client,
        token,
        linux_update_tar(version="0.6.7"),
        version="0.6.7",
        platform="linux",
        file_name="RigPilotAgent-0.6.7-Ubuntu-x86_64-OnlineUpdate.tar.gz",
    )
    assert created.status_code == 201, created.text
    agent_id, _secret = enroll_update_agent(
        client,
        token,
        name="linux-066-needs-repair",
        os_family="linux",
        version="0.6.6",
        capabilities=["agent_update.v1"],
    )

    response = client.post(
        f"/api/agent-releases/{created.json()['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    )

    assert response.status_code == 409
    assert "0.6.8 full package" in response.json()["detail"]


def test_linux_online_release_manifest_and_exact_runtime_set_are_enforced(release_app):
    client, _settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)

    for patch, expected in (
        ({"version": "0.6.7"}, "upload metadata"),
        ({"platform": "windows"}, "upload metadata"),
        ({"architecture": "arm64"}, "upload metadata"),
    ):
        response = upload(
            client,
            token,
            linux_update_tar(version="0.6.6", manifest_overrides=patch),
            version="0.6.6",
            platform="linux",
            file_name="RigPilotAgent-0.6.6-Ubuntu-x86_64-OnlineUpdate.tar.gz",
        )
        assert response.status_code == 422
        assert expected in response.json()["detail"]

    full_bootstrap = upload(
        client,
        token,
        linux_update_tar(
            version="0.6.7",
            extra_files={
                "Install.sh": b"#!/bin/sh\n",
                "rigpilot-agent.service": b"[Service]\n",
            },
        ),
        version="0.6.7",
        platform="linux",
        file_name="RigPilotAgent-0.6.7-Ubuntu-x86_64.tar.gz",
    )
    assert full_bootstrap.status_code == 422
    assert "exactly RigPilotAgent" in full_bootstrap.json()["detail"]

    bad_hash = upload(
        client,
        token,
        linux_update_tar(
            version="0.6.8",
            manifest_overrides={"files": {"RigPilotAgent": "0" * 64}},
        ),
        version="0.6.8",
        platform="linux",
        file_name="RigPilotAgent-0.6.8-Ubuntu-x86_64-OnlineUpdate.tar.gz",
    )
    assert bad_hash.status_code == 422
    assert "SHA-256" in bad_hash.json()["detail"]


def test_linux_online_release_rejects_unsafe_special_duplicate_and_sparse_members(
    tmp_path: Path,
):
    def archive_with(entries: list[tarfile.TarInfo]) -> Path:
        target = tmp_path / f"payload-{len(list(tmp_path.iterdir()))}.tar.gz"
        with tarfile.open(target, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for entry in entries:
                payload = b"x" * entry.size if entry.isfile() else None
                archive.addfile(entry, io.BytesIO(payload) if payload is not None else None)
        return target

    unsafe_names = (
        "/absolute/RigPilotAgent",
        "bundle/../RigPilotAgent",
        "bundle\\RigPilotAgent",
    )
    for name in unsafe_names:
        entry = tarfile.TarInfo(name)
        entry.size = 1
        with pytest.raises(HTTPException, match="unsafe path"):
            _validate_linux_agent_tar_gz(
                archive_with([entry]),
                max_upload_bytes=1024 * 1024,
                expected_version="0.6.6",
            )

    first = tarfile.TarInfo("bundle/RigPilotAgent")
    first.size = 1
    second = tarfile.TarInfo("bundle/RigPilotAgent")
    second.size = 1
    with pytest.raises(HTTPException, match="duplicate paths"):
        _validate_linux_agent_tar_gz(
            archive_with([first, second]),
            max_upload_bytes=1024 * 1024,
            expected_version="0.6.6",
        )

    special_types = (
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
    )
    for member_type in special_types:
        special = tarfile.TarInfo("bundle/special")
        special.type = member_type
        special.linkname = "bundle/RigPilotAgent"
        with pytest.raises(HTTPException, match="regular files and directories"):
            _validate_linux_agent_tar_gz(
                archive_with([special]),
                max_upload_bytes=1024 * 1024,
                expected_version="0.6.6",
            )

    sparse = tarfile.TarInfo("bundle/RigPilotAgent")
    sparse.type = tarfile.GNUTYPE_SPARSE
    with pytest.raises(HTTPException, match="sparse files"):
        _validate_linux_agent_tar_gz(
            archive_with([sparse]),
            max_upload_bytes=1024 * 1024,
            expected_version="0.6.6",
        )


def test_linux_online_release_rejects_expansion_and_member_count_bombs(tmp_path: Path):
    expansion = tmp_path / "expansion.tar.gz"
    expansion.write_bytes(linux_update_tar(executable=b"x" * 300_000))
    with pytest.raises(HTTPException) as expanded:
        _validate_linux_agent_tar_gz(
            expansion,
            max_upload_bytes=64 * 1024,
            expected_version="0.6.6",
        )
    assert expanded.value.status_code == 413

    many = tmp_path / "many.tar.gz"
    with tarfile.open(many, mode="w:gz") as archive:
        for index in range(16_385):
            entry = tarfile.TarInfo(f"bundle/d{index}")
            entry.type = tarfile.DIRTYPE
            archive.addfile(entry)
    with pytest.raises(HTTPException, match="entry count"):
        _validate_linux_agent_tar_gz(
            many,
            max_upload_bytes=8 * 1024 * 1024,
            expected_version="0.6.6",
        )


def test_cancelled_release_upload_removes_partial_file(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)

    async def cancel_mid_upload():
        waiting_for_next_chunk = asyncio.Event()

        async def partial_body():
            yield b"partial package"
            waiting_for_next_chunk.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://test") as http:
            pending = asyncio.create_task(http.post(
                "/api/agent-releases/upload",
                headers={**auth(token), "Content-Type": "application/octet-stream"},
                params={"version": "0.8.60", "file_name": "RigPilotAgent.zip"},
                content=partial_body(),
            ))
            try:
                await asyncio.wait_for(waiting_for_next_chunk.wait(), timeout=5)
                assert list((settings.data_dir / "agent-releases").glob(".*.uploading"))
            finally:
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending

    asyncio.run(cancel_mid_upload())
    assert not list((settings.data_dir / "agent-releases").iterdir())
    assert client.get("/api/agent-releases", headers=auth(token)).json() == []


def test_release_target_filename_archive_and_size_validation(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
        max_agent_release_bytes=4096,
    )
    with TestClient(create_app(settings)) as client:
        token = login(client, "admin", ADMIN_PASSWORD)
        valid = portable_zip()

        assert upload(client, token, valid, platform="linux").status_code == 422
        assert upload(client, token, valid, architecture="arm64").status_code == 422
        assert (
            upload(
                client,
                token,
                valid,
                file_name="../RigPilotAgent-Portable.zip",
            ).status_code
            == 422
        )
        assert (
            upload(client, token, valid, file_name="RigPilotAgent.exe").status_code
            == 422
        )

        invalid = upload(client, token, b"not a zip", version="0.6.1")
        assert invalid.status_code == 422
        assert "valid ZIP" in invalid.json()["detail"]

        missing_stream = io.BytesIO()
        with zipfile.ZipFile(missing_stream, "w") as archive:
            archive.writestr("README.txt", "missing portable executable")
        missing = upload(client, token, missing_stream.getvalue(), version="0.6.2")
        assert missing.status_code == 422
        assert "agent-release.json" in missing.json()["detail"]

        unsafe = portable_zip(extra_entries={"../escape.txt": b"no"})
        unsafe_response = upload(client, token, unsafe, version="0.6.3")
        assert unsafe_response.status_code == 422
        assert "unsafe path" in unsafe_response.json()["detail"]

        oversized = upload(client, token, b"x" * 4097, version="0.6.4")
        assert oversized.status_code == 413
        assert not list((tmp_path / "agent-releases").glob(".*.uploading"))

        # No Content-Length: the streaming counter must enforce the same
        # ceiling after chunks have started arriving and remove its staging
        # file on failure.
        streamed = client.post(
            "/api/agent-releases/upload",
            headers={**auth(token), "Content-Type": "application/octet-stream"},
            params={
                "version": "0.6.46",
                "file_name": "RigPilotAgent-0.6.46-Windows-x64-Portable.zip",
            },
            content=(chunk for chunk in (b"x" * 3000, b"y" * 3000)),
        )
        assert streamed.status_code == 413
        assert not list((tmp_path / "agent-releases").glob(".*.uploading"))

        wrong_media = client.post(
            "/api/agent-releases/upload",
            headers={**auth(token), "Content-Type": "text/plain"},
            params={
                "version": "0.6.5",
                "file_name": "RigPilotAgent-0.6.5-Windows-x64-Portable.zip",
            },
            content=valid,
        )
        assert wrong_media.status_code == 415


def test_release_zip_rejects_corruption_and_windows_path_ambiguity(release_app):
    client, _settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)

    corrupted = bytearray(portable_zip(executable=b"crc-check-agent-executable", version="0.6.10"))
    central_offset = corrupted.find(b"PK\x01\x02")
    while central_offset >= 0:
        name_length = struct.unpack_from("<H", corrupted, central_offset + 28)[0]
        entry_name = bytes(
            corrupted[central_offset + 46 : central_offset + 46 + name_length]
        ).decode("utf-8")
        if entry_name == "RigPilotAgent-Portable/RigPilotAgent.exe":
            corrupted[central_offset + 16] ^= 0x01
            break
        central_offset = corrupted.find(b"PK\x01\x02", central_offset + 4)
    assert central_offset >= 0
    damaged = upload(client, token, bytes(corrupted), version="0.6.10")
    assert damaged.status_code == 422
    assert "valid ZIP" in damaged.json()["detail"]

    unsafe_names = (
        "RigPilotAgent-Portable/state/device.json:secret",
        "RigPilotAgent-Portable/CON.txt",
        "RigPilotAgent-Portable/trailing-dot.",
    )
    for index, unsafe_name in enumerate(unsafe_names, start=11):
        response = upload(
            client,
            token,
            portable_zip(
                extra_entries={unsafe_name: b"unsafe"}, version=f"0.6.{index}"
            ),
            version=f"0.6.{index}",
        )
        assert response.status_code == 422
        assert "unsafe path" in response.json()["detail"]

    backslash_zip = portable_zip(version="0.6.19")
    safe_name = b"RigPilotAgent-Portable/RigPilotAgent.exe"
    unsafe_name = b"RigPilotAgent-Portable\\RigPilotAgent.exe"
    assert len(safe_name) == len(unsafe_name)
    assert backslash_zip.count(safe_name) >= 2
    backslash_response = upload(
        client,
        token,
        backslash_zip.replace(safe_name, unsafe_name),
        version="0.6.19",
    )
    assert backslash_response.status_code == 422
    assert "unsafe path" in backslash_response.json()["detail"]

    duplicate_stream = io.BytesIO()
    with zipfile.ZipFile(duplicate_stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("portable/RigPilotAgent.exe", b"first")
        archive.writestr("portable/Start-RigPilotAgent.bat", b"start")
        archive.writestr("PORTABLE/RIGPILOTAGENT.EXE", b"second")
    duplicate = upload(client, token, duplicate_stream.getvalue(), version="0.6.20")
    assert duplicate.status_code == 422
    assert "duplicate Windows paths" in duplicate.json()["detail"]

    unsupported_stream = io.BytesIO()
    with zipfile.ZipFile(unsupported_stream, "w", compression=zipfile.ZIP_BZIP2) as archive:
        archive.writestr("portable/RigPilotAgent.exe", b"agent")
        archive.writestr("portable/Start-RigPilotAgent.bat", b"start")
    unsupported = upload(client, token, unsupported_stream.getvalue(), version="0.6.21")
    assert unsupported.status_code == 422
    assert "unsupported compression" in unsupported.json()["detail"]


def test_duplicate_target_version_is_rejected_without_orphan(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    payload = portable_zip()

    first = upload(client, token, payload)
    assert first.status_code == 201, first.text
    duplicate = upload(
        client,
        token,
        payload,
        file_name="RigPilotAgent-renamed-Windows-x64-Portable.zip",
        architecture="amd64",
    )
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]
    assert len(client.get("/api/agent-releases", headers=auth(token)).json()) == 1
    root = settings.data_dir / "agent-releases"
    assert not list(root.glob(".*.uploading"))
    assert len([item for item in root.iterdir() if item.is_dir()]) == 1


def test_agent_release_storage_path_cannot_escape_data_directory(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="agent-release-storage-path-test-key-is-long-enough",
        admin_username="admin",
        admin_password=ADMIN_PASSWORD,
    )
    with pytest.raises(HTTPException) as caught:
        _agent_release_storage_path(settings, "../outside.zip")
    assert caught.value.status_code == 500


def test_agent_update_is_single_device_capability_gated_and_agent_authenticated(
    release_app,
):
    client, settings = release_app
    admin_token = login(client, "admin", ADMIN_PASSWORD)
    client.post(
        "/api/users",
        headers=auth(admin_token),
        json={
            "username": "release-operator",
            "password": ADMIN_PASSWORD,
            "role": "operator",
        },
    )
    operator_token = login(client, "release-operator", ADMIN_PASSWORD)
    release_payload = portable_zip(executable=b"version-0.5.7", version="0.5.7")
    release_response = upload(
        client,
        admin_token,
        release_payload,
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    )
    assert release_response.status_code == 201, release_response.text
    release = release_response.json()

    old_id, _ = enroll_update_agent(
        client,
        admin_token,
        name="old-agent",
        version="0.5.6",
        capabilities=[],
    )
    no_capability_id, _ = enroll_update_agent(
        client,
        admin_token,
        name="no-capability",
        version="0.5.7",
        capabilities=[],
    )
    linux_id, _ = enroll_update_agent(
        client,
        admin_token,
        name="linux-agent",
        os_family="linux",
        version="0.5.7",
        capabilities=["agent_update.v1"],
    )
    offline_id, _ = enroll_update_agent(
        client,
        admin_token,
        name="offline-agent",
        version="0.5.7",
        capabilities=["agent_update.v1"],
        heartbeat=False,
    )
    canary_id, canary_secret = enroll_update_agent(
        client,
        admin_token,
        version="0.5.6",
        capabilities=["AGENT_UPDATE.V1", "agent_update.v1"],
    )
    other_id, other_secret = enroll_update_agent(
        client,
        admin_token,
        name="other-canary",
        version="0.5.6",
        capabilities=["agent_update.v1"],
    )

    agents = client.get("/api/agents", headers=auth(admin_token)).json()
    canary = next(item for item in agents if item["id"] == canary_id)
    assert canary["capabilities"] == ["agent_update.v1"]

    deploy_url = f"/api/agent-releases/{release['id']}/deploy"
    assert client.post(deploy_url, json={"agent_id": canary_id}).status_code == 401
    assert (
        client.post(
            deploy_url,
            headers=auth(operator_token),
            json={"agent_id": canary_id},
        ).status_code
        == 403
    )
    for incompatible_id, expected in (
        (old_id, "does not advertise"),
        (no_capability_id, "does not advertise"),
        (linux_id, "Windows"),
        (offline_id, "online"),
    ):
        response = client.post(
            deploy_url,
            headers=auth(admin_token),
            json={"agent_id": incompatible_id},
        )
        assert response.status_code == 409, response.text
        assert expected in response.json()["detail"]

    queued = client.post(
        deploy_url,
        headers=auth(admin_token),
        json={"agent_id": canary_id},
    )
    assert queued.status_code == 201, queued.text
    update = queued.json()
    assert update["agent_id"] == canary_id
    assert update["source_version"] == "0.5.6"
    assert update["target_version"] == "0.5.7"
    assert update["status"] == "queued"

    duplicate = client.post(
        deploy_url,
        headers=auth(admin_token),
        json={"agent_id": canary_id},
    )
    assert duplicate.status_code == 409
    assert "already in progress" in duplicate.json()["detail"]

    command_id = update["command_id"]
    expected_download_url = f"/api/agent/updates/{command_id}/download"
    with sqlite3.connect(settings.database_path) as db:
        stored_payload = db.execute(
            "SELECT payload_json FROM commands WHERE id=?", (command_id,)
        ).fetchone()[0]
    assert json.loads(stored_payload) == {
        "release_id": release["id"],
        "version": "0.5.7",
        "sha256": release["sha256"],
        "size": len(release_payload),
        "download_url": expected_download_url,
    }

    canary_headers = {
        "X-Agent-ID": canary_id,
        "Authorization": f"Bearer {canary_secret}",
    }
    other_headers = {
        "X-Agent-ID": other_id,
        "Authorization": f"Bearer {other_secret}",
    }
    assert client.get(expected_download_url, headers=canary_headers).status_code == 409
    assert client.get(expected_download_url, headers=other_headers).status_code == 404

    heartbeat = client.post(
        "/api/agent/heartbeat",
        headers=canary_headers,
        json={
            "hostname": "update-canary.local",
            "os_family": "windows",
            "platform": "Windows 10 x64",
            "agent_version": "0.5.6",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["commands"] == [
        {
            "id": command_id,
            "action": "agent_update.v1",
            "payload": json.loads(stored_payload),
        }
    ]
    other_poll = client.post(
        "/api/agent/heartbeat",
        headers=other_headers,
        json={
            "hostname": "other-canary.local",
            "os_family": "windows",
            "platform": "Windows 10 x64",
            "agent_version": "0.5.6",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    assert other_poll.status_code == 200
    assert other_poll.json()["commands"] == []

    downloaded = client.get(expected_download_url, headers=canary_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == release_payload
    assert downloaded.headers["x-checksum-sha256"] == release["sha256"]
    assert downloaded.headers["cache-control"] == "private, no-store"

    updates = client.get("/api/agent-updates", headers=auth(admin_token))
    assert updates.status_code == 200, updates.text
    assert updates.json()[0]["status"] == "dispatched"
    assert updates.json()[0]["downloaded_at"] is not None
    assert client.get("/api/agent-updates", headers=auth(operator_token)).status_code == 403

    running = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=canary_headers,
        json={"status": "running", "result": {"stage": "activating"}},
    )
    assert running.status_code == 200
    assert (
        client.get("/api/agent-updates", headers=auth(admin_token)).json()[0]["status"]
        == "dispatched"
    )

    activation_id = "a" * 32
    staged = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=canary_headers,
        json={
            "status": "succeeded",
            "result": {
                "state": "staged",
                "release_id": release["id"],
                "version": "0.5.7",
                "sha256": release["sha256"],
                "activation_id": activation_id,
                "activation_scheduled": True,
                "restart_required": True,
                "miner_continuity_checked": True,
            },
        },
    )
    assert staged.status_code == 200, staged.text
    assert staged.json() == {"status": "running"}
    assert client.get("/api/agent-updates", headers=auth(admin_token)).json()[0]["status"] == "activating"

    # Once staged, neither the old process nor the new one receives the same
    # activation command again.  Both carry command_id locally for final ack.
    old_poll = client.post(
        "/api/agent/heartbeat",
        headers=canary_headers,
        json={
            "hostname": "update-canary.local",
            "os_family": "windows",
            "platform": "Windows 10 x64",
            "agent_version": "0.5.6",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    assert old_poll.status_code == 200
    assert old_poll.json()["commands"] == []

    for expected_count in (1, 2):
        reconnected = client.post(
            "/api/agent/heartbeat",
            headers=canary_headers,
            json={
                "hostname": "update-canary.local",
                "os_family": "windows",
                "platform": "Windows 10 x64",
                "agent_version": "0.5.7",
                "capabilities": ["agent_update.v1"],
                "inventory": {"architecture": "AMD64"},
            },
        )
        assert reconnected.status_code == 200, reconnected.text
        assert reconnected.json()["commands"] == []
        pending = client.get("/api/agent-updates", headers=auth(admin_token)).json()[0]
        assert pending["status"] == "activating"
        assert pending["target_heartbeat_count"] == expected_count
        if expected_count == 1:
            premature = client.post(
                f"/api/agent/commands/{command_id}/result",
                headers=canary_headers,
                json={
                    "status": "succeeded",
                    "result": {
                        "activation_confirmed": True,
                        "activation_id": activation_id,
                        "version": "0.5.7",
                        "health_heartbeats": 2,
                    },
                },
            )
            assert premature.status_code == 409

    confirmed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=canary_headers,
        json={
            "status": "succeeded",
            "result": {
                "activation_confirmed": True,
                "activation_id": activation_id,
                "version": "0.5.7",
                "health_heartbeats": 2,
            },
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"status": "succeeded"}
    finished = client.get("/api/agent-updates", headers=auth(admin_token)).json()[0]
    assert finished["status"] == "succeeded"
    assert finished["completed_at"] is not None
    command = client.get(
        f"/api/commands/{command_id}", headers=auth(admin_token)
    ).json()
    assert command["status"] == "succeeded"
    assert command["result"]["activation_confirmed"] is True
    late_failure = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=canary_headers,
        json={
            "status": "failed",
            "result": {
                "activation_id": activation_id,
                "version": "0.5.7",
                "rolled_back": True,
            },
            "error": "late stale rollback receipt",
        },
    )
    assert late_failure.status_code == 200
    assert late_failure.json() == {"status": "succeeded"}
    assert client.get("/api/agent-updates", headers=auth(admin_token)).json()[0]["status"] == "succeeded"

    protected = client.delete(
        f"/api/agent-releases/{release['id']}", headers=auth(admin_token)
    )
    assert protected.status_code == 409
    assert "update history" in protected.json()["detail"]
    audit_actions = {
        item["action"]
        for item in client.get("/api/audit?limit=100", headers=auth(admin_token)).json()
    }
    assert {
        "agent_update_queued",
        "agent_update_download_started",
        "agent_update_activating",
        "agent_update_target_connected",
        "agent_update_succeeded",
    } <= audit_actions


def test_agent_update_download_rejects_payload_binding_tamper(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    release = upload(
        client,
        token,
        portable_zip(version="0.5.8"),
        version="0.5.8",
        file_name="RigPilotAgent-0.5.8-Windows-x64-Portable.zip",
    ).json()
    agent_id, secret = enroll_update_agent(
        client,
        token,
        capabilities=["agent_update.v1"],
    )
    queued = client.post(
        f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    ).json()
    headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"}
    client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "update-canary.local",
            "os_family": "windows",
            "agent_version": "0.5.7",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "UPDATE commands SET payload_json='{}' WHERE id=?",
            (queued["command_id"],),
        )
        db.commit()
    response = client.get(
        f"/api/agent/updates/{queued['command_id']}/download", headers=headers
    )
    assert response.status_code == 409
    assert "binding is invalid" in response.json()["detail"]


def test_activating_update_ignores_expiry_and_accepts_bound_rollback_result(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    release = upload(
        client,
        token,
        portable_zip(version="0.5.7"),
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    ).json()
    agent_id, secret = enroll_update_agent(
        client,
        token,
        version="0.5.6",
        capabilities=["agent_update.v1"],
    )
    headers = {"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"}
    queued = client.post(
        f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    ).json()
    command_id = queued["command_id"]
    client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "update-canary.local",
            "os_family": "windows",
            "agent_version": "0.5.6",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    activation_id = "b" * 32
    staged_result = {
        "state": "staged",
        "release_id": release["id"],
        "version": "0.5.7",
        "sha256": release["sha256"],
        "activation_id": activation_id,
        "activation_scheduled": True,
        "restart_required": True,
        "miner_continuity_checked": True,
    }
    staged = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=headers,
        json={"status": "succeeded", "result": staged_result},
    )
    assert staged.status_code == 200
    # A duplicate staged acknowledgement is idempotent and must not reset
    # target heartbeat health accounting.
    assert (
        client.post(
            f"/api/agent/commands/{command_id}/result",
            headers=headers,
            json={"status": "succeeded", "result": staged_result},
        ).json()
        == {"status": "running"}
    )
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "UPDATE commands SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (command_id,),
        )
        db.commit()
    activation_poll = client.post(
        "/api/agent/heartbeat",
        headers=headers,
        json={
            "hostname": "update-canary.local",
            "os_family": "windows",
            "agent_version": "0.5.6",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    assert activation_poll.status_code == 200
    assert activation_poll.json()["commands"] == []
    assert client.get("/api/agent-updates", headers=auth(token)).json()[0]["status"] == "activating"

    unconfirmed = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=headers,
        json={
            "status": "failed",
            "result": {
                "activation_id": activation_id,
                "version": "0.5.7",
                "rolled_back": False,
            },
            "error": "not a confirmed rollback",
        },
    )
    assert unconfirmed.status_code == 409
    wrong_binding = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=headers,
        json={
            "status": "failed",
            "result": {
                "activation_id": "c" * 32,
                "version": "0.5.7",
                "rolled_back": True,
            },
            "error": "wrong activation",
        },
    )
    assert wrong_binding.status_code == 409
    assert client.get("/api/agent-updates", headers=auth(token)).json()[0]["status"] == "activating"

    rollback = client.post(
        f"/api/agent/commands/{command_id}/result",
        headers=headers,
        json={
            "status": "failed",
            "result": {
                "activation_id": activation_id,
                "version": "0.5.7",
                "rolled_back": True,
            },
            "error": "new Agent did not become healthy; restored previous slot",
        },
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json() == {"status": "failed"}
    failed = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert failed["status"] == "failed"
    assert "restored previous slot" in failed["error"]
    actions = {
        item["action"]
        for item in client.get("/api/audit?limit=100", headers=auth(token)).json()
    }
    assert "agent_update_rollback_reported" in actions


def test_activation_timeout_uses_activation_transition_not_queued_age(release_app):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    release = upload(
        client,
        token,
        portable_zip(version="0.5.7"),
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    ).json()
    agent_id, _secret = enroll_update_agent(
        client,
        token,
        version="0.5.6",
        capabilities=["agent_update.v1"],
    )
    queued = client.post(
        f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    ).json()
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            """UPDATE agent_update_jobs
               SET status='activating', created_at='2000-01-01T00:00:00+00:00',
                   updated_at='2999-01-01T00:00:00+00:00'
               WHERE command_id=?""",
            (queued["command_id"],),
        )
        db.execute(
            "UPDATE commands SET status='running' WHERE id=?",
            (queued["command_id"],),
        )
    fresh = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert fresh["status"] == "activating"

    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            """UPDATE agent_update_jobs
               SET updated_at='2000-01-01T00:00:00+00:00'
               WHERE command_id=?""",
            (queued["command_id"],),
        )
    expired = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert expired["status"] == "failed"
    assert "45 minutes" in expired["error"]


def test_update_is_revalidated_against_dispatch_heartbeat(release_app):
    client, _settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    release = upload(
        client,
        token,
        portable_zip(version="0.5.8"),
        version="0.5.8",
        file_name="RigPilotAgent-0.5.8-Windows-x64-Portable.zip",
    ).json()
    agent_id, secret = enroll_update_agent(
        client,
        token,
        version="0.5.7",
        capabilities=["agent_update.v1"],
    )
    queued = client.post(
        f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    )
    assert queued.status_code == 201, queued.text
    heartbeat_response = client.post(
        "/api/agent/heartbeat",
        headers={"X-Agent-ID": agent_id, "Authorization": f"Bearer {secret}"},
        json={
            "hostname": "changed-before-dispatch.local",
            "os_family": "windows",
            "platform": "Windows 11",
            "agent_version": "0.5.9",
            "capabilities": ["agent_update.v1"],
            "inventory": {"architecture": "AMD64"},
        },
    )
    assert heartbeat_response.status_code == 200
    assert heartbeat_response.json()["commands"] == []
    update = client.get("/api/agent-updates", headers=auth(token)).json()[0]
    assert update["status"] == "failed"
    assert "version changed" in update["error"]


def test_agent_release_manifest_version_hash_and_data_are_enforced(release_app):
    client, _settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)

    mismatch = upload(
        client,
        token,
        portable_zip(version="0.5.8"),
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    )
    assert mismatch.status_code == 422
    assert "does not match upload metadata" in mismatch.json()["detail"]

    with_data = upload(
        client,
        token,
        portable_zip(version="0.5.9", extra_entries={"data/device.json": b"secret"}),
        version="0.5.9",
        file_name="RigPilotAgent-0.5.9-Windows-x64-Portable.zip",
    )
    assert with_data.status_code == 422
    assert "device data" in with_data.json()["detail"]

    source = zipfile.ZipFile(io.BytesIO(portable_zip(version="0.6.0")))
    entries = {item.filename: source.read(item) for item in source.infolist()}
    manifest_name = "RigPilotAgent-Portable/agent-release.json"
    manifest = json.loads(entries[manifest_name])
    manifest["files"]["RigPilotAgent.exe"] = "0" * 64
    entries[manifest_name] = json.dumps(manifest).encode()
    bad_hash_stream = io.BytesIO()
    with zipfile.ZipFile(bad_hash_stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    bad_hash = upload(client, token, bad_hash_stream.getvalue(), version="0.6.0")
    assert bad_hash.status_code == 422


def test_agent_update_rejects_same_version_and_downgrade(release_app):
    client, _settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    same_release = upload(
        client,
        token,
        portable_zip(version="0.5.7"),
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    ).json()
    older_release = upload(
        client,
        token,
        portable_zip(version="0.5.6"),
        version="0.5.6",
        file_name="RigPilotAgent-0.5.6-Windows-x64-Portable.zip",
    ).json()
    agent_id, _secret = enroll_update_agent(
        client,
        token,
        version="0.5.7",
        capabilities=["agent_update.v1"],
    )
    same = client.post(
        f"/api/agent-releases/{same_release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    )
    assert same.status_code == 409
    assert "already runs" in same.json()["detail"]
    downgrade = client.post(
        f"/api/agent-releases/{older_release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    )
    assert downgrade.status_code == 409
    assert "downgrade" in downgrade.json()["detail"]


def test_pre_protocol_repository_row_remains_downloadable_but_not_deployable(
    release_app,
):
    client, settings = release_app
    token = login(client, "admin", ADMIN_PASSWORD)
    release = upload(
        client,
        token,
        portable_zip(version="0.5.7"),
        version="0.5.7",
        file_name="RigPilotAgent-0.5.7-Windows-x64-Portable.zip",
    ).json()
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            "UPDATE agent_releases SET update_protocol=NULL WHERE id=?",
            (release["id"],),
        )
        db.commit()
    listed = client.get("/api/agent-releases", headers=auth(token)).json()[0]
    assert listed["deployable"] is False
    assert listed["update_protocol"] is None
    assert client.get(listed["download_url"], headers=auth(token)).status_code == 200
    agent_id, _secret = enroll_update_agent(
        client,
        token,
        version="0.5.6",
        capabilities=["agent_update.v1"],
    )
    blocked = client.post(
        f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(token),
        json={"agent_id": agent_id},
    )
    assert blocked.status_code == 409
    assert "download-only" in blocked.json()["detail"]
