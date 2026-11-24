from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_test_support import auth, login as _login

from server.config import Settings
from server.database import utcnow
from server.main import create_app
from server.managed_miners import (
    ManagedMinerCatalog,
    MINER_TYPES,
    _bounded_getaddrinfo,
)


def release_documents(owner: str, repo: str) -> list[dict[str, object]]:
    if owner == "xmrig":
        versions = ("7.0.2", "7.0.1", "7.0.0")
    elif owner == "doktor83":
        versions = ("4.0.2", "4.0.1", "4.0.0")
    elif owner == "pearlfortune":
        versions = ("1.2.4", "1.2.3", "1.2.2")
    else:
        raise AssertionError(f"unexpected managed miner repository: {owner}/{repo}")
    documents: list[dict[str, object]] = []
    for version in versions:
        if owner == "xmrig":
            tag = f"v{version}"
            filenames = (
                f"xmrig-{version}-windows-x64.zip",
                f"xmrig-{version}-linux-static-x64.tar.gz",
            )
        elif owner == "doktor83":
            tag = version
            dashed = version.replace(".", "-")
            filenames = (
                f"SRBMiner-Multi-{dashed}-win64.zip",
                f"SRBMiner-Multi-{dashed}-Linux.tar.gz",
            )
        elif owner == "pearlfortune":
            tag = f"v{version}"
            filenames = (
                f"miner-windows-v{version}.zip",
                f"pearlfortune-v{version}.tar.gz",
                f"pearlfortune-amd-v{version}.tar.gz",
            )
        else:
            raise AssertionError(f"unexpected managed miner repository: {owner}/{repo}")
        documents.append(
            {
                "tag_name": tag,
                "draft": False,
                "prerelease": False,
                "assets": [
                    {
                        "name": filename,
                        "browser_download_url": (
                            f"https://github.com/{owner}/{repo}/releases/download/"
                            f"{tag}/{filename}"
                        ),
                        "digest": "sha256:" + "a" * 64,
                        "size": 1024,
                    }
                    for filename in filenames
                ],
            }
        )
    return documents


def test_catalog_reads_are_nonblocking_and_refresh_is_single_flight():
    entered = threading.Event()
    release_fetch = threading.Event()
    calls: list[tuple[str, str]] = []
    calls_lock = threading.Lock()

    def slow_fetcher(owner: str, repo: str):
        with calls_lock:
            calls.append((owner, repo))
        entered.set()
        if not release_fetch.wait(2):
            raise TimeoutError("test fetch gate timed out")
        return release_documents(owner, repo)

    catalog = ManagedMinerCatalog(fetcher=slow_fetcher, ttl_seconds=60)
    started = time.monotonic()
    initial = catalog.payload()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert {item["catalog_source"] for item in initial} == {"fallback"}
    assert entered.wait(1)
    # While the first repository fetch is blocked, repeated readers must not
    # start another refresh or wait for the existing one.
    for _ in range(20):
        assert catalog.payload()[0]["latest_version"] == "6.26.0"
    with calls_lock:
        assert len(calls) == 1

    release_fetch.set()
    catalog.refresh_sync(force=False, timeout=2)
    updated = {item["id"]: item for item in catalog.payload()}
    assert updated["xmrig"]["latest_version"] == "7.0.2"
    assert updated["srbminer"]["latest_version"] == "4.0.2"
    assert updated["pearlminer"]["latest_version"] == "1.2.4"
    assert set(updated) == {"xmrig", "srbminer", "pearlminer", "seine"}
    # The stub fetcher serves no zpalmtree/seine releases, so Seine keeps its
    # pinned fallback snapshot while the other three move to github data.
    assert updated["seine"]["latest_version"] == "0.2.45"
    assert updated["seine"]["catalog_source"] == "fallback"
    assert {item["catalog_source"] for item in updated.values()} == {
        "github",
        "fallback",
    }
    with calls_lock:
        assert len(calls) == len(MINER_TYPES)


def test_failed_background_refresh_preserves_last_good_snapshot():
    catalog = ManagedMinerCatalog(fetcher=release_documents, failure_ttl_seconds=60)
    catalog.refresh_sync()
    assert catalog.latest_version("xmrig") == "7.0.2"

    entered = threading.Event()

    def unavailable(owner: str, repo: str):
        del owner, repo
        entered.set()
        raise OSError("offline")

    catalog._fetcher = unavailable
    catalog.invalidate()
    stale = catalog.payload()
    assert {item["latest_version"] for item in stale} == {
        "7.0.2",
        "4.0.2",
        "1.2.4",
        "0.2.45",
    }
    assert entered.wait(1)
    catalog.refresh_sync(force=False, timeout=2)
    after_failure = {item["id"]: item for item in catalog.payload()}
    assert after_failure["xmrig"]["latest_version"] == "7.0.2"
    assert after_failure["srbminer"]["latest_version"] == "4.0.2"
    assert after_failure["pearlminer"]["latest_version"] == "1.2.4"
    assert set(after_failure) == {"xmrig", "srbminer", "pearlminer", "seine"}


def test_stalled_platform_dns_lookup_has_a_firm_timeout(monkeypatch):
    blocked = threading.Event()
    calls = 0

    def stalled(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        blocked.wait()
        return [
            (
                2,
                1,
                6,
                "",
                ("192.0.2.44", 443),
            )
        ]

    monkeypatch.setattr("server.managed_miners.socket.getaddrinfo", stalled)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        _bounded_getaddrinfo(
            "api.github.com", 443, timeout_seconds=0.02
        )
    assert time.monotonic() - started < 0.5
    assert calls == 1

    # A second caller must reuse the unfinished lookup and fail immediately,
    # rather than leaking another permanently blocked resolver thread.
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="still pending"):
        _bounded_getaddrinfo(
            "api.github.com", 443, timeout_seconds=1.0
        )
    assert time.monotonic() - started < 0.2
    assert calls == 1

    # Once the original resolver finishes, its result is reusable and the
    # global single-flight slot becomes available for a future lookup.
    blocked.set()
    deadline = time.monotonic() + 1.0
    while True:
        try:
            answers = _bounded_getaddrinfo(
                "api.github.com", 443, timeout_seconds=1.0
            )
            break
        except RuntimeError as exc:
            if "still pending" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    assert answers[0][4][0] == "192.0.2.44"
    assert calls == 1
    assert _bounded_getaddrinfo(
        "api.github.com", 443, timeout_seconds=1.0
    )
    assert calls == 2


@pytest.fixture()
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="test-secret-key-that-is-long-and-random-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient) -> str:
    return _login(client, 'admin', 'correct-horse-battery-staple')


def test_managed_package_delete_protects_version_and_active_command_references(
    client: TestClient, tmp_path: Path
):
    token = login(client)
    wallet = client.post(
        "/api/wallets",
        headers=auth(token),
        json={
            "name": "public payout",
            "coin": "XMR",
            "address": "4" + "A" * 94,
        },
    ).json()
    now = utcnow()
    packages = {
        "primary": ("managed-primary", "xmrig", "6.25.0", "linux", "tar.gz"),
        "secondary": (
            "managed-secondary",
            "srbminer",
            "3.4.4",
            "windows",
            "zip",
        ),
    }
    stored_paths: dict[str, Path] = {}
    with client.app.state.db.connection() as db:
        db.execute(
            """INSERT INTO flight_sheets
               (id,name,miner_type,miner_version,coin,wallet_id,pool_url,
                miner_profile,worker_name_template,secondary_miner_type,
                secondary_miner_version,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "managed-version-reference",
                "managed versions",
                "xmrig",
                "6.25.0",
                "XMR",
                wallet["id"],
                "stratum+ssl://pool.example.test:443",
                "default",
                "{hostname}",
                "srbminer",
                "3.4.4",
                1,
                now,
                now,
            ),
        )
        for label, (package_id, miner_type, version, os_family, archive_type) in packages.items():
            filename = f"{package_id}.{archive_type}"
            storage_key = f"{package_id}/{filename}"
            path = tmp_path / "miner-packages" / storage_key
            path.parent.mkdir(parents=True)
            path.write_bytes(package_id.encode())
            stored_paths[label] = path
            db.execute(
                """INSERT INTO miner_packages
                   (id,name,version,download_url,sha256,archive_type,note,
                    source_type,storage_key,original_filename,file_size,
                    managed_miner_type,managed_os_family,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    package_id,
                    miner_type,
                    version,
                    f"/api/agent/miner-packages/{package_id}/download",
                    "",
                    archive_type,
                    "managed cache",
                    "uploaded",
                    storage_key,
                    filename,
                    path.stat().st_size,
                    miner_type,
                    os_family,
                    1,
                    now,
                    now,
                ),
            )
        # A manual package with the same name/version keeps the original
        # package-id/FK deletion semantics and is not version-pinned.
        db.execute(
            """INSERT INTO miner_packages
               (id,name,version,download_url,sha256,archive_type,note,
                source_type,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "manual-same-version",
                "xmrig",
                "6.25.0",
                "https://downloads.example.test/xmrig.zip",
                "",
                "zip",
                "manual",
                "external",
                1,
                now,
                now,
            ),
        )

    manual = client.delete(
        "/api/miner-packages/manual-same-version", headers=auth(token)
    )
    assert manual.status_code == 200, manual.text

    for package_id in ("managed-primary", "managed-secondary"):
        protected = client.delete(
            f"/api/miner-packages/{package_id}", headers=auth(token)
        )
        assert protected.status_code == 409
        assert "version is used by a flight sheet" in protected.json()["detail"]
    assert all(path.is_file() for path in stored_paths.values())

    issued = client.post(
        "/api/enrollment-tokens",
        headers=auth(token),
        json={"label": "delete-test", "ttl_minutes": 30},
    ).json()
    enrolled = client.post(
        "/api/agent/enroll",
        json={
            "enrollment_token": issued["token"],
            "hostname": "delete-test.local",
            "os_family": "linux",
        },
    ).json()
    with client.app.state.db.connection() as db:
        db.execute("DELETE FROM flight_sheets WHERE id='managed-version-reference'")
        db.execute(
            """INSERT INTO commands
               (id,agent_id,action,payload_json,status,created_by,created_at,expires_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "managed-active-reference",
                enrolled["agent_id"],
                "apply_flight_sheet",
                json.dumps(
                    {"primary": {"miner_package": {"id": "managed-primary"}}}
                ),
                "running",
                1,
                now,
                "2999-01-01T00:00:00+00:00",
            ),
        )

    active = client.delete(
        "/api/miner-packages/managed-primary", headers=auth(token)
    )
    assert active.status_code == 409
    assert "unfinished command" in active.json()["detail"]
    assert stored_paths["primary"].is_file()

    with client.app.state.db.connection() as db:
        db.execute(
            "UPDATE commands SET status='succeeded' WHERE id='managed-active-reference'"
        )
    assert (
        client.delete(
            "/api/miner-packages/managed-primary", headers=auth(token)
        ).status_code
        == 200
    )
    assert not stored_paths["primary"].exists()
    assert (
        client.delete(
            "/api/miner-packages/managed-secondary", headers=auth(token)
        ).status_code
        == 200
    )
    assert not stored_paths["secondary"].exists()
