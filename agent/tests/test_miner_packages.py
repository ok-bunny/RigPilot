from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from types import SimpleNamespace
import zipfile
from pathlib import Path

import pytest

import agent.miner_packages as miner_packages_module
from agent.config import AgentConfig
from agent.miner_packages import (
    MinerPackageError,
    install_miner_package,
    validate_download_url,
    validate_package_metadata,
)
from agent.state import AgentState


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {"Content-Length": str(len(body))}
        self.closed = False

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self):
        self.closed = True


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def tar_gz_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def metadata(body: bytes, **overrides):
    value = {
        "id": "safe-miner",
        "name": "Safe Miner",
        "version": "1.2.3",
        "download_url": "https://downloads.example.test/miner.zip",
        "sha256": hashlib.sha256(body).hexdigest(),
        "archive_type": "zip",
    }
    value.update(overrides)
    return value


def config(tmp_path: Path, **overrides):
    values = {
        "server_url": "https://controller.example.test",
        "state_file": tmp_path / "state.json",
        "allow_miner_downloads": True,
        "miner_packages_dir": tmp_path / "packages",
        "miner_download_host_allowlist": frozenset({"downloads.example.test"}),
        "miner_download_max_mb": 1,
    }
    values.update(overrides)
    return AgentConfig(**values)


def requester_for(body: bytes, *, status: int = 200, headers=None):
    calls = []
    response = FakeResponse(body, status=status, headers=headers)

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return response

    return request, response, calls


def test_windows_plain_file_binding_tolerates_cross_api_metadata_drift():
    path = SimpleNamespace(
        st_dev=17, st_ino=23, st_mode=stat.S_IFREG | 0o600,
        st_nlink=1, st_size=8, st_mtime_ns=100, st_ctime_ns=200,
        st_file_attributes=0x20,
    )
    handle = SimpleNamespace(
        st_dev=17, st_ino=23, st_mode=stat.S_IFREG | 0o400,
        st_nlink=1, st_size=8, st_mtime_ns=101, st_ctime_ns=201,
        st_file_attributes=0x80,
    )
    assert miner_packages_module._same_opened_file(path, handle, windows=True)
    assert not miner_packages_module._same_opened_file(path, handle, windows=False)


@pytest.mark.parametrize(
    "change",
    (
        {"st_dev": 0},
        {"st_ino": 0},
        {"st_ino": 24},
        {"st_nlink": 2},
        {"st_size": 9},
        {"st_mode": stat.S_IFDIR | 0o700},
        {"st_file_attributes": 0x400},
    ),
)
def test_windows_plain_file_binding_rejects_replacement_links_and_reparse(change):
    path = SimpleNamespace(
        st_dev=17, st_ino=23, st_mode=stat.S_IFREG | 0o600,
        st_nlink=1, st_size=8, st_mtime_ns=100, st_ctime_ns=200,
        st_file_attributes=0x20,
    )
    handle = SimpleNamespace(**(vars(path) | change))
    assert not miner_packages_module._same_opened_file(path, handle, windows=True)


def write_legacy_package_cache(
    package: dict,
    cfg: AgentConfig,
    *,
    content: bytes = b"legacy cached executable",
) -> Path:
    assert cfg.miner_packages_dir is not None
    digest_suffix = f"-{package['sha256'][:12]}" if package["sha256"] else ""
    family_suffix = (
        f"-{package['managed_os_family']}"
        if package.get("managed_os_family") in {"windows", "linux"}
        else ""
    )
    target = cfg.miner_packages_dir / (
        f"{package['id']}-{package['version']}{family_suffix}{digest_suffix}"
    )
    target.mkdir(parents=True)
    (target / "miner.exe").write_bytes(content)
    marker = dict(package)
    marker.update(miner_packages_module._tree_fingerprint(target, 1024 * 1024))
    marker["downloaded_bytes"] = len(content)
    miner_packages_module._write_marker(target / ".rigpilot-package.json", marker)
    return target


def test_download_url_requires_allowlisted_https_or_loopback_http():
    hosts = frozenset({"downloads.example.test", "127.0.0.1"})
    assert (
        validate_download_url("https://downloads.example.test/a.zip", hosts)
        == "https://downloads.example.test/a.zip"
    )
    assert (
        validate_download_url("http://127.0.0.1:8080/a.zip", hosts)
        == "http://127.0.0.1:8080/a.zip"
    )
    with pytest.raises(MinerPackageError, match="allowlist"):
        validate_download_url("https://evil.example/a.zip", hosts)
    with pytest.raises(MinerPackageError, match="HTTPS"):
        validate_download_url("http://downloads.example.test/a.zip", hosts)
    with pytest.raises(MinerPackageError, match="credentials"):
        validate_download_url("https://user:pass@downloads.example.test/a.zip", hosts)
    with pytest.raises(MinerPackageError, match="malformed"):
        validate_download_url("https://[::1/a.zip", hosts)


def test_package_install_streams_and_reuses_verified_content_tree(tmp_path: Path):
    body = zip_bytes({"miner/readme.txt": b"public test package", "miner/bin.dat": b"x"})
    request, response, calls = requester_for(body)
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    assert package is not None
    result = install_miner_package(package, cfg, requester=request)
    installed = Path(result["path"])
    assert result["downloaded"] is True
    assert (installed / "miner" / "readme.txt").read_bytes() == b"public test package"
    assert response.closed is True
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["allow_redirects"] is False
    if hasattr(stat, "S_IXUSR"):
        assert not ((installed / "miner" / "bin.dat").stat().st_mode & stat.S_IXUSR)

    def no_second_download(*args, **kwargs):
        raise AssertionError("immutable package should be reused")

    reused = install_miner_package(package, cfg, requester=no_second_download)
    assert reused["downloaded"] is False
    assert reused["path"] == result["path"]
    marker = json.loads((installed / ".rigpilot-package.json").read_text("utf-8"))
    assert marker["verified_archive_sha256_v1"] == package["sha256"]
    assert len(marker["verified_archive_sha256_v1"]) == 64


def test_managed_os_families_never_share_one_package_cache_directory(tmp_path: Path):
    body = zip_bytes({"miner/bin": b"same release identifier, different target OS"})
    cfg = config(tmp_path)
    windows = validate_package_metadata(
        metadata(body, managed_os_family="windows"), cfg
    )
    linux = validate_package_metadata(
        metadata(body, managed_os_family="linux"), cfg
    )
    windows_request, _, _ = requester_for(body)
    linux_request, _, _ = requester_for(body)

    windows_result = install_miner_package(windows, cfg, requester=windows_request)
    linux_result = install_miner_package(linux, cfg, requester=linux_request)

    assert windows_result["path"] != linux_result["path"]
    assert Path(windows_result["path"]).name.endswith("-windows-" + windows["sha256"][:12])
    assert Path(linux_result["path"]).name.endswith("-linux-" + linux["sha256"][:12])


def test_controller_package_uses_agent_auth(tmp_path: Path):
    body = zip_bytes({"miner/readme.txt": b"controller package"})
    cfg = config(
        tmp_path,
        miner_download_host_allowlist=frozenset(),
    )
    package = validate_package_metadata(
        metadata(
            body,
            download_url="/api/agent/miner-packages/safe-miner/download",
        ),
        cfg,
    )
    assert package is not None
    assert package["download_url"] == (
        "https://controller.example.test/api/agent/miner-packages/"
        "safe-miner/download"
    )
    request, _, calls = requester_for(body)
    state = AgentState.new("agent-123", "secret-456")

    installed = install_miner_package(package, cfg, requester=request, state=state)

    assert installed["downloaded"] is True
    assert calls[0][1]["headers"] == {
        "X-Agent-ID": "agent-123",
        "Authorization": "Bearer secret-456",
    }


def test_controller_package_requires_state_and_never_leaks_auth_cross_origin(
    tmp_path: Path,
):
    body = zip_bytes({"miner.txt": b"package"})
    controller_cfg = config(tmp_path, miner_download_host_allowlist=frozenset())
    controller_package = validate_package_metadata(
        metadata(
            body,
            download_url="/api/agent/miner-packages/safe-miner/download",
        ),
        controller_cfg,
    )
    assert controller_package is not None
    request, _, calls = requester_for(body)
    with pytest.raises(MinerPackageError, match="requires Agent authentication"):
        install_miner_package(controller_package, controller_cfg, requester=request)
    assert calls == []

    external_cfg = config(
        tmp_path / "external",
        miner_download_host_allowlist=frozenset({"downloads.example.test"}),
    )
    external_package = validate_package_metadata(metadata(body), external_cfg)
    assert external_package is not None
    external_request, _, external_calls = requester_for(body)
    install_miner_package(
        external_package,
        external_cfg,
        requester=external_request,
        state=AgentState.new("agent", "must-not-leak"),
    )
    assert "headers" not in external_calls[0][1]


def test_controller_package_path_must_match_metadata_id(tmp_path: Path):
    body = zip_bytes({"miner.txt": b"package"})
    cfg = config(tmp_path, miner_download_host_allowlist=frozenset())
    with pytest.raises(MinerPackageError, match="hostname"):
        validate_package_metadata(
            metadata(
                body,
                download_url="/api/agent/miner-packages/another-id/download",
            ),
            cfg,
        )


def test_declared_sha256_is_normalized_and_verified_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    body = zip_bytes({"miner.txt": b"hello"})
    cfg = config(tmp_path)
    expected_digest = hashlib.sha256(body).hexdigest()
    package = validate_package_metadata(
        metadata(body, sha256=f" {expected_digest.upper()} "), cfg
    )
    assert package["sha256"] == expected_digest
    request, response, _ = requester_for(body)
    installed = install_miner_package(package, cfg, requester=request)
    assert installed["downloaded"] is True
    assert response.closed is True

    bad_package = validate_package_metadata(
        metadata(body, id="wrong-digest", sha256="0" * 64), cfg
    )
    bad_request, bad_response, _ = requester_for(body)
    extraction_called = False

    def fail_if_extracted(*args, **kwargs):
        nonlocal extraction_called
        extraction_called = True
        raise AssertionError("digest mismatch must fail before archive extraction")

    monkeypatch.setattr(miner_packages_module, "_extract_zip", fail_if_extracted)
    with pytest.raises(MinerPackageError, match="SHA-256"):
        install_miner_package(bad_package, cfg, requester=bad_request)
    assert extraction_called is False
    assert bad_response.closed is True


def test_digest_bound_legacy_marker_is_redownloaded_without_overwriting_old_cache(
    tmp_path: Path,
):
    body = zip_bytes({"miner.exe": b"verified replacement"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    legacy = write_legacy_package_cache(package, cfg)
    request, _, calls = requester_for(body)

    installed = install_miner_package(package, cfg, requester=request)

    assert installed["downloaded"] is True
    assert calls
    assert Path(installed["path"]) != legacy
    assert Path(installed["path"]).name.startswith(
        ".rigpilot-verified-sha256-v1-"
    )
    assert (legacy / "miner.exe").read_bytes() == b"legacy cached executable"
    replacement_marker = json.loads(
        (Path(installed["path"]) / ".rigpilot-package.json").read_text("utf-8")
    )
    assert replacement_marker["verified_archive_sha256_v1"] == package["sha256"]


def test_different_canonical_attestation_is_redownloaded_side_by_side(
    tmp_path: Path,
):
    body = zip_bytes({"miner.exe": b"verified replacement"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    legacy = write_legacy_package_cache(package, cfg)
    marker_path = legacy / ".rigpilot-package.json"
    marker = json.loads(marker_path.read_text("utf-8"))
    different_digest = ("f" if package["sha256"][0] != "f" else "e") + package[
        "sha256"
    ][1:]
    marker["verified_archive_sha256_v1"] = different_digest
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    request, _, calls = requester_for(body)

    installed = install_miner_package(package, cfg, requester=request)

    assert installed["downloaded"] is True
    assert calls
    assert Path(installed["path"]) != legacy
    assert json.loads(marker_path.read_text("utf-8"))[
        "verified_archive_sha256_v1"
    ] == different_digest


def test_digest_bound_side_cache_reuses_only_matching_full_attestation(tmp_path: Path):
    body = zip_bytes({"miner.exe": b"verified replacement"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    legacy = write_legacy_package_cache(package, cfg)
    request, _, _ = requester_for(body)
    first = install_miner_package(package, cfg, requester=request)

    def no_download(*args, **kwargs):
        raise AssertionError("matching full archive attestation should be reused")

    reused = install_miner_package(package, cfg, requester=no_download)

    assert reused["downloaded"] is False
    assert reused["path"] == first["path"]
    assert Path(reused["path"]) != legacy


def test_mismatched_side_cache_attestation_is_rejected_before_download(
    tmp_path: Path,
):
    body = zip_bytes({"miner.exe": b"verified replacement"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    write_legacy_package_cache(package, cfg)
    request, _, _ = requester_for(body)
    first = install_miner_package(package, cfg, requester=request)
    marker_path = Path(first["path"]) / ".rigpilot-package.json"
    marker = json.loads(marker_path.read_text("utf-8"))
    marker["verified_archive_sha256_v1"] = "f" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    second_request, _, second_calls = requester_for(body)

    with pytest.raises(MinerPackageError, match="verified archive SHA-256"):
        install_miner_package(package, cfg, requester=second_request)

    assert second_calls == []


def test_concurrent_side_cache_winner_requires_matching_full_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    body = zip_bytes({"miner.exe": b"verified replacement"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    write_legacy_package_cache(package, cfg)
    request, _, _ = requester_for(body)
    real_replace = miner_packages_module.os.replace

    def competing_unverified_replace(source: Path, destination: Path):
        destination.mkdir()
        (destination / "miner.exe").write_bytes(b"competing unverified cache")
        marker = dict(package)
        marker.update(
            miner_packages_module._tree_fingerprint(destination, 1024 * 1024)
        )
        marker["downloaded_bytes"] = 1
        miner_packages_module._write_marker(
            destination / ".rigpilot-package.json", marker
        )
        raise OSError("simulated concurrent winner")

    monkeypatch.setattr(miner_packages_module.os, "replace", competing_unverified_replace)
    with pytest.raises(MinerPackageError, match="verified archive SHA-256"):
        install_miner_package(package, cfg, requester=request)
    monkeypatch.setattr(miner_packages_module.os, "replace", real_replace)


def test_redirect_and_size_limit_fail_closed(
    tmp_path: Path,
):
    body = zip_bytes({"miner.txt": b"hello"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    redirect, redirect_response, _ = requester_for(b"", status=302)
    with pytest.raises(MinerPackageError, match="redirect"):
        install_miner_package(package, cfg, requester=redirect)
    assert redirect_response.closed is True

    oversized = b"x" * (1024 * 1024 + 1)
    oversized_package = validate_package_metadata(metadata(oversized), cfg)
    too_large, _, _ = requester_for(oversized, headers={})
    with pytest.raises(MinerPackageError, match="size limit"):
        install_miner_package(oversized_package, cfg, requester=too_large)


def test_package_sha_is_optional_only_for_unbound_legacy_metadata(tmp_path: Path):
    body = zip_bytes({"miner.txt": b"hello"})
    cfg = config(tmp_path)

    # Missing/null digests remain temporarily supported for historical rows;
    # downloads through this path are explicitly not cryptographically bound.
    without_hash = metadata(body)
    without_hash.pop("sha256")
    assert validate_package_metadata(without_hash, cfg)["sha256"] is None
    assert validate_package_metadata(metadata(body, sha256=None), cfg)["sha256"] is None
    unbound = validate_package_metadata(without_hash, cfg)
    legacy = write_legacy_package_cache(unbound, cfg)

    def no_download(*args, **kwargs):
        raise AssertionError("unbound historical caches remain compatible")

    reused = install_miner_package(unbound, cfg, requester=no_download)
    assert reused["downloaded"] is False
    assert reused["path"] == str(legacy)
    marker = json.loads((legacy / ".rigpilot-package.json").read_text("utf-8"))
    assert "verified_archive_sha256_v1" not in marker


@pytest.mark.parametrize(
    "digest",
    ["", "not-a-hash", "0" * 63, "0" * 65, "g" * 64, b"0" * 64],
)
def test_invalid_package_sha256_metadata_is_rejected(tmp_path: Path, digest):
    body = zip_bytes({"miner.txt": b"hello"})
    cfg = config(tmp_path)

    with pytest.raises(MinerPackageError, match="sha256"):
        validate_package_metadata(metadata(body, sha256=digest), cfg)


def test_bad_zip_crc_is_still_rejected_after_sha256_verification(tmp_path: Path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("miner.exe", b"known-payload")
    corrupted = bytearray(output.getvalue())
    offset = corrupted.index(b"known-payload")
    corrupted[offset] ^= 0x01
    body = bytes(corrupted)
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    request, _, _ = requester_for(body)

    with pytest.raises(MinerPackageError, match="miner ZIP archive is invalid"):
        install_miner_package(package, cfg, requester=request)


def test_controller_origin_change_reuses_verified_package_cache(tmp_path: Path):
    body = zip_bytes({"miner/readme.txt": b"controller package"})
    old_cfg = config(tmp_path, miner_download_host_allowlist=frozenset())
    raw = metadata(
        body,
        download_url="/api/agent/miner-packages/safe-miner/download",
    )
    old_package = validate_package_metadata(raw, old_cfg)
    request, _, _ = requester_for(body)
    state = AgentState.new("agent", "secret")
    installed = install_miner_package(old_package, old_cfg, requester=request, state=state)

    new_cfg = config(
        tmp_path,
        server_url="https://new-controller.example.test",
        miner_download_host_allowlist=frozenset(),
    )
    new_package = validate_package_metadata(raw, new_cfg)

    def no_download(*args, **kwargs):
        raise AssertionError("verified package cache should be reused after origin change")

    reused = install_miner_package(
        new_package, new_cfg, requester=no_download, state=state
    )
    assert reused["downloaded"] is False
    assert reused["path"] == installed["path"]


def test_zip_traversal_and_symlink_are_never_extracted(tmp_path: Path):
    traversal = zip_bytes({"../escape.txt": b"never"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(traversal), cfg)
    request, _, _ = requester_for(traversal)
    with pytest.raises(MinerPackageError, match="unsafe member"):
        install_miner_package(package, cfg, requester=request)
    assert not (tmp_path / "escape.txt").exists()

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        member = zipfile.ZipInfo("miner-link")
        member.create_system = 3
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "../outside")
    symlink_body = output.getvalue()
    symlink_package = validate_package_metadata(
        metadata(symlink_body, id="symlink-miner"), cfg
    )
    symlink_request, _, _ = requester_for(symlink_body)
    with pytest.raises(MinerPackageError, match="symbolic links"):
        install_miner_package(symlink_package, cfg, requester=symlink_request)


@pytest.mark.parametrize(
    "member_name",
    [
        "dir/file.txt:stream",
        "dir/C:/escape.txt",
        "dir/CON.txt",
        "dir/NUL",
        "dir/COM1.log",
        "dir/COM¹.log",
        "dir/trailing.",
        "dir/trailing ",
        r"C:\\escape.txt",
        r"\\\\server\\share\\escape.txt",
    ],
)
def test_windows_ambiguous_archive_paths_are_rejected(
    tmp_path: Path, member_name: str
):
    body = zip_bytes({member_name: b"never"})
    cfg = config(tmp_path)
    package = validate_package_metadata(
        metadata(body, id="ambiguous-path-miner"), cfg
    )
    request, _, _ = requester_for(body)
    with pytest.raises(MinerPackageError):
        install_miner_package(package, cfg, requester=request)


def test_tar_links_and_special_files_are_rejected(tmp_path: Path):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        link = tarfile.TarInfo("miner-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        archive.addfile(link)
    body = output.getvalue()
    cfg = config(tmp_path)
    package = validate_package_metadata(
        metadata(body, id="tar-miner", archive_type="tar.gz"), cfg
    )
    request, _, _ = requester_for(body)
    with pytest.raises(MinerPackageError, match="links"):
        install_miner_package(package, cfg, requester=request)

    special_output = io.BytesIO()
    with tarfile.open(fileobj=special_output, mode="w:gz") as archive:
        special = tarfile.TarInfo("device")
        special.type = tarfile.CHRTYPE
        archive.addfile(special)
    special_body = special_output.getvalue()
    special_package = validate_package_metadata(
        metadata(
            special_body,
            id="tar-special-miner",
            archive_type="tar.gz",
        ),
        cfg,
    )
    special_request, _, _ = requester_for(special_body)
    with pytest.raises(MinerPackageError, match="special file"):
        install_miner_package(special_package, cfg, requester=special_request)


def test_tar_member_limit_is_enforced_while_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for index in range(3):
            member = tarfile.TarInfo(f"file-{index}")
            member.size = 1
            archive.addfile(member, io.BytesIO(b"x"))
    body = output.getvalue()
    cfg = config(tmp_path)
    package = validate_package_metadata(
        metadata(body, id="many-members", archive_type="tar.gz"), cfg
    )
    request, _, _ = requester_for(body)
    monkeypatch.setattr(miner_packages_module, "_MAX_MEMBERS", 2)
    with pytest.raises(MinerPackageError, match="too many members"):
        install_miner_package(package, cfg, requester=request)


def tar_path_entries(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, kind, content in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(content)
            member.mode = 0
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                member.linkname = "../outside"
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def install_tar_path_entries(tmp_path: Path, body: bytes):
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body, archive_type="tar.gz"), cfg)
    request, _, _ = requester_for(body)
    return install_miner_package(package, cfg, requester=request)


@pytest.mark.parametrize("root_name", [".", "./"])
def test_tar_current_directory_prefix_installs_verified_files(tmp_path: Path, root_name: str):
    body = tar_path_entries([
        (root_name, tarfile.DIRTYPE, b""),
        ("./README.md", tarfile.REGTYPE, b"public readme"),
        ("./miner", tarfile.DIRTYPE, b""),
        ("././miner/seine", tarfile.REGTYPE, b"synthetic miner"),
    ])
    installed = install_tar_path_entries(tmp_path, body)
    cache = Path(installed["path"])
    assert (cache / "README.md").read_bytes() == b"public readme"
    assert (cache / "miner" / "seine").read_bytes() == b"synthetic miner"
    marker = json.loads((cache / ".rigpilot-package.json").read_text())
    assert marker["file_count"] == 2
    assert marker["extracted_bytes"] == len(b"public readmesynthetic miner")
    assert installed["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("name", [
    "./../escape", "./miner/../escape", "./miner/./bin", "./miner//bin",
    "././/bin", "././", ".//bin", ".\\bin", "./.\\bin", "/bin", "//host/bin",
    "./C:/bin", "./bin:stream", "./bin.", "./bin ", "./CON.txt",
    "./COM¹.log", "./.rigpilot-package.json", "./bin/\x01control",
    "./\\\\host\\bin",
])
def test_tar_prefix_does_not_relax_unsafe_member_paths(tmp_path: Path, name: str):
    body = tar_gz_bytes({name: b"never"})
    with pytest.raises(MinerPackageError):
        install_tar_path_entries(tmp_path, body)
    assert not (tmp_path / "escape").exists()


@pytest.mark.parametrize("first,second", [
    ("miner", "./miner"), ("./miner", "miner"), ("./miner", "././miner"),
])
def test_tar_normalized_duplicates_cannot_overwrite(tmp_path: Path, first: str, second: str):
    body = tar_path_entries([
        (first, tarfile.REGTYPE, b"first"),
        (second, tarfile.REGTYPE, b"second"),
    ])
    with pytest.raises(MinerPackageError, match="duplicate member paths"):
        install_tar_path_entries(tmp_path, body)


def test_tar_root_alias_duplicate_is_rejected(tmp_path: Path):
    body = tar_path_entries([
        (".", tarfile.DIRTYPE, b""), ("./", tarfile.DIRTYPE, b""),
        ("./miner", tarfile.REGTYPE, b"miner"),
    ])
    with pytest.raises(MinerPackageError, match="duplicate member paths"):
        install_tar_path_entries(tmp_path, body)


@pytest.mark.parametrize("root_name", [".", "./"])
@pytest.mark.parametrize("kind,content", [
    (tarfile.REGTYPE, b""), (tarfile.REGTYPE, b"data"),
    (tarfile.DIRTYPE, b"data"), (tarfile.SYMTYPE, b""),
    (tarfile.LNKTYPE, b""), (tarfile.FIFOTYPE, b""),
])
def test_tar_root_is_only_an_empty_directory(
    tmp_path: Path, root_name: str, kind: bytes, content: bytes,
):
    body = tar_path_entries([(root_name, kind, content)])
    with pytest.raises(MinerPackageError, match="root path must be an empty directory"):
        install_tar_path_entries(tmp_path, body)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_tar_prefixed_links_and_special_members_still_fail(tmp_path: Path, kind: bytes):
    body = tar_path_entries([("./miner", kind, b"")])
    with pytest.raises(MinerPackageError, match="links|special file"):
        install_tar_path_entries(tmp_path, body)


def test_tar_root_noop_still_consumes_member_budget(tmp_path: Path, monkeypatch):
    body = tar_path_entries([
        (".", tarfile.DIRTYPE, b""),
        ("./first", tarfile.REGTYPE, b"1"), ("./second", tarfile.REGTYPE, b"2"),
    ])
    monkeypatch.setattr(miner_packages_module, "_MAX_MEMBERS", 2)
    with pytest.raises(MinerPackageError, match="too many members"):
        install_tar_path_entries(tmp_path, body)


@pytest.mark.parametrize("limit,value,message", [
    ("_MAX_PATH_DEPTH", 2, "too deep"), ("_MAX_MEMBER_PATH", 8, "unsafe member path"),
])
def test_tar_prefix_counts_toward_original_path_budgets(
    tmp_path: Path, monkeypatch, limit: str, value: int, message: str,
):
    body = tar_gz_bytes({"././seine": b"miner"})
    monkeypatch.setattr(miner_packages_module, limit, value)
    with pytest.raises(MinerPackageError, match=message):
        install_tar_path_entries(tmp_path, body)


def test_tar_root_noop_does_not_change_destination_permissions(tmp_path: Path):
    archive = tmp_path / "miner.tar.gz"
    archive.write_bytes(tar_path_entries([
        (".", tarfile.DIRTYPE, b""), ("./miner", tarfile.REGTYPE, b"miner"),
    ]))
    destination = tmp_path / "content"
    destination.mkdir(mode=0o710)
    original_mode = stat.S_IMODE(destination.stat().st_mode)
    assert miner_packages_module._extract_tar_gz(archive, destination, 100) == 5
    assert stat.S_IMODE(destination.stat().st_mode) == original_mode


def test_tar_prefix_does_not_relax_expanded_size_budget(tmp_path: Path):
    archive = tmp_path / "miner.tar.gz"
    archive.write_bytes(tar_gz_bytes({"./miner": b"too large"}))
    destination = tmp_path / "content"
    destination.mkdir()
    with pytest.raises(MinerPackageError, match="size limit"):
        miner_packages_module._extract_tar_gz(archive, destination, 2)


def test_zip_current_directory_prefix_remains_rejected(tmp_path: Path):
    body = zip_bytes({"./miner": b"never"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    request, _, _ = requester_for(body)
    with pytest.raises(MinerPackageError, match="unsafe member path"):
        install_miner_package(package, cfg, requester=request)


def test_native_sealed_cache_rejects_added_runtime_file_without_rewriting_marker(
    tmp_path: Path,
):
    executable_bytes = b"synthetic native miner"
    body = zip_bytes({"miner/SRBMiner-MULTI": executable_bytes})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body, managed_os_family="linux"), cfg)
    assert package is not None
    request, _, calls = requester_for(body)
    installed = install_miner_package(package, cfg, requester=request)
    cache = Path(installed["path"])
    marker = cache / ".rigpilot-package.json"
    original_marker = marker.read_bytes()
    runtime_file = cache / "miner" / "generated-kernel.bin"
    runtime_file.write_bytes(b"synthetic runtime-generated kernel")

    def no_second_download(*args, **kwargs):
        raise AssertionError("changed sealed cache must fail before another download")

    with pytest.raises(MinerPackageError, match="content does not match its marker"):
        install_miner_package(package, cfg, requester=no_second_download)

    assert len(calls) == 1
    assert marker.read_bytes() == original_marker
    assert (cache / "miner" / "SRBMiner-MULTI").read_bytes() == executable_bytes
    assert runtime_file.read_bytes() == b"synthetic runtime-generated kernel"


def test_reuse_verifies_installed_content_and_total_download_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    body = zip_bytes({"miner/bin.dat": b"original"})
    cfg = config(tmp_path)
    package = validate_package_metadata(metadata(body), cfg)
    request, _, _ = requester_for(body)
    installed = install_miner_package(package, cfg, requester=request)
    (Path(installed["path"]) / "miner" / "bin.dat").write_bytes(b"tampered")
    with pytest.raises(MinerPackageError, match="content does not match"):
        install_miner_package(package, cfg, requester=lambda *args, **kwargs: None)

    timed_body = zip_bytes({"miner/file": b"x"})
    timed_package = validate_package_metadata(
        metadata(timed_body, id="slow-miner"), cfg
    )
    timed_request, timed_response, _ = requester_for(timed_body)
    moments = iter((0.0, 0.0, cfg.miner_download_total_timeout + 1.0))
    monkeypatch.setattr(miner_packages_module.time, "monotonic", moments.__next__)
    with pytest.raises(MinerPackageError, match="total time limit"):
        install_miner_package(timed_package, cfg, requester=timed_request)
    assert timed_response.closed is True


def test_package_metadata_defaults_to_local_opt_in(tmp_path: Path):
    body = zip_bytes({"miner.txt": b"hello"})
    disabled = AgentConfig(
        server_url="https://controller.example.test",
        state_file=tmp_path / "state.json",
    )
    with pytest.raises(MinerPackageError, match="disabled locally"):
        validate_package_metadata(metadata(body), disabled)


@pytest.mark.parametrize("miner_type", ["lolminer", "forgeminer"])
@pytest.mark.parametrize("namespace", ["builtin", "test"])
def test_retired_packages_reject_validation_download_and_cache_reuse(tmp_path, miner_type, namespace):
    cfg = config(tmp_path)
    body = zip_bytes({"retired-program": b"archived only"})
    package = metadata(body)
    package["id"] = f"{namespace}-{miner_type}-1.0-linux-x64"
    cache = write_legacy_package_cache(package, cfg)
    before = {str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob("*") if path.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail("retired package must not reach any downloader")
    with pytest.raises(MinerPackageError, match="adapter is retired"):
        validate_package_metadata(package, cfg)
    with pytest.raises(MinerPackageError, match="adapter is retired"):
        install_miner_package(package, cfg, requester=forbidden)
    after = {str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("display_name", ["lolminer", "forgeminer"])
@pytest.mark.parametrize("package_id", ["generic-fixture", "same-as-display-name"])
def test_generic_package_display_name_and_short_id_do_not_claim_builtin_adapter(tmp_path, display_name, package_id):
    cfg = config(tmp_path)
    body = zip_bytes({"generic-program": b"generic archive fixture"})
    package = metadata(body, id=display_name if package_id == "same-as-display-name" else package_id,
                       name=display_name)
    validated = validate_package_metadata(package, cfg)
    assert validated["name"] == display_name and validated["id"] == package["id"]
    request, response, calls = requester_for(body)
    installed = install_miner_package(validated, cfg, requester=request)
    assert installed["downloaded"] is True and len(calls) == 1
    assert (Path(installed["path"]) / "generic-program").read_bytes() == b"generic archive fixture"
    reused = install_miner_package(validated, cfg, requester=lambda *_a, **_k: pytest.fail("verified generic cache must be reused"))
    assert reused["downloaded"] is False and reused["path"] == installed["path"]

