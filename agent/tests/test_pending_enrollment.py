from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import state as module

CONTROLLER = "https://controller.example"
TOKEN = "enrollment-token-do-not-log"
METADATA = {"hostname": "original-host", "os_family": "Linux", "platform": "linux-x64", "agent_version": "0.8.18"}


def test_pending_enrollment_is_private_stable_and_independent(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("existing state untouched")
    replacement = module.replacement_pending_path(path)
    replacement.write_text("replacement untouched")
    first = module.load_or_create_pending_enrollment(path, CONTROLLER + "/", TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    raw = json.loads(journal.read_text())
    assert raw == {
        "version": 1, "request_id": first.request_id,
        "proposed_agent_secret": first.proposed_agent_secret,
        "controller_url": CONTROLLER,
        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "metadata": METADATA,
    }
    assert len(first.request_id) == 32
    assert len(first.proposed_agent_secret) == 52
    assert first.proposed_agent_secret not in repr(first)
    assert TOKEN not in journal.read_text() and TOKEN not in repr(first)
    if os.name != "nt":
        assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert journal.stat().st_uid == os.geteuid()
    assert module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA) == first
    assert module.load_pending_enrollment(path, CONTROLLER) == first
    assert path.read_text() == "existing state untouched"
    assert replacement.read_text() == "replacement untouched"
    assert list(tmp_path.glob("*.tmp")) == []


def test_pending_enrollment_survives_a_new_python_process(tmp_path):
    path = tmp_path / "state.json"
    first = module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    source = (
        "from pathlib import Path; import hashlib, json, sys; "
        "from agent.state import load_pending_enrollment; "
        "p=load_pending_enrollment(Path(sys.argv[1]),sys.argv[2]); "
        "print(json.dumps([p.request_id,hashlib.sha256(p.proposed_agent_secret.encode()).hexdigest()]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", source, str(path), CONTROLLER],
        capture_output=True, text=True, check=True,
        cwd=Path(module.__file__).resolve().parents[1],
    )
    assert json.loads(completed.stdout) == [
        first.request_id, hashlib.sha256(first.proposed_agent_secret.encode()).hexdigest(),
    ]
    assert first.proposed_agent_secret not in completed.stdout


@pytest.mark.parametrize("controller,token", [("https://different.example", TOKEN), (CONTROLLER, "different-token"), (CONTROLLER, "")])
def test_pending_enrollment_binding_mismatch_never_overwrites(tmp_path, controller, token):
    path = tmp_path / "state.json"
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    before = journal.read_bytes()
    with pytest.raises(module.StateError):
        module.load_or_create_pending_enrollment(path, controller, token, METADATA)
    with pytest.raises(module.StateError):
        module.load_pending_enrollment(path, controller, token)
    assert journal.read_bytes() == before


def test_pending_absent_is_none_and_tokenless_load_never_creates(tmp_path):
    path = tmp_path / "missing" / "state.json"
    assert module.load_pending_enrollment(path, CONTROLLER) is None
    assert not path.parent.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync ordering")
def test_pending_publication_fsyncs_complete_file_before_link_and_directory_before_return(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    events = []
    real_fsync, real_link = os.fsync, os.link
    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            assert stat.S_IMODE(info.st_mode) == 0o600
            events.append("file_fsync")
        else:
            events.append("directory_fsync")
        real_fsync(fd)
    def link(source, destination):
        assert events == ["file_fsync"]
        raw = json.loads(Path(source).read_text())
        assert raw["token_sha256"] == hashlib.sha256(TOKEN.encode()).hexdigest()
        assert not Path(destination).exists()
        events.append("publish")
        real_link(source, destination)
    monkeypatch.setattr(module.os, "fsync", fsync)
    monkeypatch.setattr(module.os, "link", link)
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    assert events == ["file_fsync", "publish", "directory_fsync"]


def test_concurrent_creators_return_the_same_complete_journal(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    barrier = threading.Barrier(4)
    real_link = os.link
    def link(source, destination):
        barrier.wait(timeout=10)
        real_link(source, destination)
    monkeypatch.setattr(module.os, "link", link)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(module.load_or_create_pending_enrollment, path, CONTROLLER, TOKEN, METADATA) for _ in range(4)]
        results = [future.result(timeout=15) for future in futures]
    assert all(result == results[0] for result in results)
    assert module.load_pending_enrollment(path, CONTROLLER, TOKEN) == results[0]
    assert list(tmp_path.glob("*.tmp")) == []


def test_crash_window_hardlink_alias_does_not_change_replayed_credentials(tmp_path):
    path = tmp_path / "state.json"
    first = module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    os.link(module.enrollment_pending_path(path), tmp_path / "leftover.tmp")
    assert module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA) == first


@pytest.mark.parametrize("kind", ["malformed", "oversize", "directory", "symlink", "dangling_symlink"])
def test_invalid_or_unsafe_journal_is_preserved_and_rejected(tmp_path, kind):
    path = tmp_path / "state.json"
    journal = module.enrollment_pending_path(path)
    if kind == "directory":
        journal.mkdir()
    elif "symlink" in kind:
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_text("private unrelated file")
        try:
            journal.symlink_to(target)
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP} or getattr(exc, "winerror", None) == 1314:
                pytest.skip(f"symlink creation is unavailable: {exc}")
            raise
    else:
        journal.write_bytes(b"not-json" if kind == "malformed" else b"x" * (64 * 1024 + 1))
        journal.chmod(0o600)
    before = journal.lstat()
    with pytest.raises(module.StateError):
        module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    assert journal.lstat().st_ino == before.st_ino


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode")
@pytest.mark.parametrize("kind", ["mode", "owner"])
def test_pending_rejects_wrong_owner_or_permissions(tmp_path, monkeypatch, kind):
    path = tmp_path / "state.json"
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    if kind == "mode":
        journal.chmod(0o640)
    else:
        original = Path.lstat
        def lstat(value):
            info = original(value)
            if value != journal:
                return info
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_uid"] += 1
            return SimpleNamespace(**fields)
        monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(module.StateError, match="not private"):
        module.load_pending_enrollment(path, CONTROLLER)


def test_pending_rejects_windows_reparse_attribute_even_on_a_regular_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    original = Path.lstat
    def lstat(value):
        info = original(value)
        if value != journal:
            return info
        fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        fields["st_file_attributes"] = 0x400
        return SimpleNamespace(**fields)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(module.StateError, match="not private"):
        module.load_pending_enrollment(path, CONTROLLER)


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("request_id", "A" * 32),
    ("proposed_agent_secret", "invalid"), ("token_sha256", "g" * 64),
    ("extra", "unexpected"),
])
def test_pending_rejects_corrupt_credential_schema(tmp_path, field, value):
    path = tmp_path / "state.json"
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    raw = json.loads(journal.read_text())
    raw[field] = value
    journal.write_text(json.dumps(raw))
    with pytest.raises(module.StateError):
        module.load_pending_enrollment(path, CONTROLLER)


def test_file_sync_failure_never_publishes_and_redacts_os_error(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    sensitive = "rpa_" + "Z" * 48
    def fail(_fd):
        raise OSError(TOKEN + sensitive)
    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(module.StateError) as caught:
        module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered and sensitive not in rendered
    assert not module.enrollment_pending_path(path).exists()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
def test_directory_sync_failure_preserves_published_nonce_for_retry(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    real_sync = module._fsync_directory
    def fail(_directory):
        raise OSError("directory fsync failed")
    monkeypatch.setattr(module, "_fsync_directory", fail)
    with pytest.raises(module.StateError):
        module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    published = module.load_pending_enrollment(path, CONTROLLER, TOKEN)
    assert published is not None
    monkeypatch.setattr(module, "_fsync_directory", real_sync)
    assert module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA) == published


def test_unsupported_exclusive_publication_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    def fail(*_args):
        raise OSError("hard links unavailable")
    monkeypatch.setattr(module.os, "link", fail)
    with pytest.raises(module.StateError):
        module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    assert not module.enrollment_pending_path(path).exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_clear_removes_only_own_journal_and_fsyncs_directory(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("saved final state")
    replacement = module.replacement_pending_path(path)
    replacement.write_text("replacement journal")
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    calls = []
    real_sync = module._fsync_directory
    def sync(directory):
        assert not module.enrollment_pending_path(path).exists()
        calls.append(directory)
        real_sync(directory)
    monkeypatch.setattr(module, "_fsync_directory", sync)
    module.clear_pending_enrollment(path)
    assert calls == [tmp_path]
    assert path.read_text() == "saved final state"
    assert replacement.read_text() == "replacement journal"


def test_metadata_retains_first_wire_values_across_restarts_and_input_changes(tmp_path):
    path = tmp_path / "state.json"
    metadata = {**METADATA, "name": " first name "}
    first = module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, metadata)
    assert first.metadata == metadata
    metadata["hostname"] = "changed-after-creation"
    assert first.metadata["hostname"] == "original-host"
    changed = {**METADATA, "hostname": "new-host", "agent_version": "99.0", "name": "new-name"}
    repeated = module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, changed)
    assert repeated == first
    assert repeated.metadata["name"] == " first name "
    assert module.load_pending_enrollment(path, CONTROLLER).metadata == first.metadata


@pytest.mark.parametrize("metadata", [
    {}, {**METADATA, "unexpected": "field"}, {**METADATA, "name": ""},
    {**METADATA, "hostname": 123}, {**METADATA, "name": "x" * 4097},
    {key: "x" * 4096 for key in METADATA},
])
def test_invalid_metadata_never_creates_a_journal(tmp_path, metadata):
    path = tmp_path / "state.json"
    with pytest.raises(module.StateError, match="metadata"):
        module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, metadata)
    assert not module.enrollment_pending_path(path).exists()


def test_persisted_unknown_metadata_field_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    raw = json.loads(journal.read_text())
    raw["metadata"]["unknown"] = "field"
    journal.write_text(json.dumps(raw))
    with pytest.raises(module.StateError, match="metadata"):
        module.load_pending_enrollment(path, CONTROLLER)


def test_tokenless_existing_pending_is_synced_before_return(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    first = module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    before = journal.read_bytes()
    calls = []
    real_sync = module._fsync_directory
    def sync(directory):
        calls.append(directory)
        real_sync(directory)
    monkeypatch.setattr(module, "_fsync_directory", sync)
    assert module.load_or_create_pending_enrollment(path, CONTROLLER, None, {}) == first
    assert calls == [tmp_path]
    assert journal.read_bytes() == before


def test_tokenless_existing_pending_sync_failure_retains_same_credentials(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    first = module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
    journal = module.enrollment_pending_path(path)
    before = journal.read_bytes()
    real_sync = module._fsync_directory
    def fail(_directory):
        raise OSError("directory fsync failed")
    monkeypatch.setattr(module, "_fsync_directory", fail)
    with pytest.raises(module.StateError, match="made durable"):
        module.load_or_create_pending_enrollment(path, CONTROLLER, None, METADATA)
    assert journal.read_bytes() == before
    monkeypatch.setattr(module, "_fsync_directory", real_sync)
    assert module.load_or_create_pending_enrollment(path, CONTROLLER, None, METADATA) == first


def test_tokenless_missing_pending_rejects_without_creating_or_hashing(tmp_path, monkeypatch):
    path = tmp_path / "missing" / "state.json"
    def unexpected_digest(_token):
        pytest.fail("tokenless lookup must not hash or invent a token")
    monkeypatch.setattr(module, "_enrollment_token_digest", unexpected_digest)
    with pytest.raises(module.StateError, match="requires a token"):
        module.load_or_create_pending_enrollment(path, CONTROLLER, None, METADATA)
    assert not path.parent.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["state", "enrollment", "replacement"])
def test_unparseable_integer_is_invalid_state_and_never_overwritten(tmp_path, kind):
    path = tmp_path / "state.json"
    journal = {"state": path, "enrollment": module.enrollment_pending_path(path),
               "replacement": module.replacement_pending_path(path)}[kind]
    raw = ('{"invalid":' + '9' * 5000 + '}').encode()
    journal.write_bytes(raw)
    journal.chmod(0o600)
    with pytest.raises(module.StateError):
        if kind == "state":
            module.load_state(path)
        elif kind == "enrollment":
            module.load_or_create_pending_enrollment(path, CONTROLLER, TOKEN, METADATA)
        else:
            module.load_or_create_pending_replacement(path, CONTROLLER, "rpr_" + "T" * 43)
    assert journal.read_bytes() == raw
