"""Bound, private replacement retries and install-mode conflicts without network I/O."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from agent import state as state_store
from agent.client import ProtocolError
from agent.config import AgentConfig
from agent.runtime import AgentRuntime
from agent.state import AgentState, StateError

CONTROLLER = "https://controller.example"
TOKEN = "rpr_" + "T" * 43
OTHER_TOKEN = "rpr_" + "U" * 43
AGENT_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HOST = {"hostname": "replacement-host", "os_family": "Linux", "platform": "linux-x64"}
METADATA = {**HOST, "agent_version": "0.8.22"}


class RecordingClient:
    def __init__(self, *, lose_first_response: bool = False, wrong_secret: bool = False):
        self.calls: list[tuple[str, str, str]] = []
        self.enrollment_calls: list[str | None] = []
        self.lose_first_response = lose_first_response
        self.wrong_secret = wrong_secret

    def replace_identity(self, token, host, *, request_id, proposed_agent_secret):
        assert host == HOST
        self.calls.append((token, request_id, proposed_agent_secret))
        if self.lose_first_response:
            self.lose_first_response = False
            raise RuntimeError("synthetic response loss")
        secret = "unexpected-secret" if self.wrong_secret else proposed_agent_secret
        return AgentState.new(AGENT_ID, secret)

    def enroll(self, token, host, **_pending):
        self.enrollment_calls.append(token)
        raise AssertionError("ordinary enrollment POST must not occur")


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    monkeypatch.delenv("RIGPILOT_ENROLLMENT_TOKEN", raising=False)
    monkeypatch.delenv("RIGPILOT_REPLACEMENT_TOKEN", raising=False)
    monkeypatch.setattr("agent.runtime.collect_host_data", lambda *_args, **_kwargs: dict(HOST))
    config = AgentConfig(server_url=CONTROLLER, state_file=tmp_path / "state.json")
    created = []

    def new(config_override=None, client=None):
        runtime = AgentRuntime(config_override or config)
        runtime._host_data = lambda: dict(HOST)
        runtime.client = client or RecordingClient()
        created.append(runtime)
        return runtime

    yield config, new
    for runtime in created:
        assert runtime._host_data_sampler.close(timeout=1)


def test_replacement_journal_is_private_bound_and_stable(tmp_path):
    state_path = tmp_path / "state.json"
    first = state_store.load_or_create_pending_replacement(state_path, CONTROLLER + "/", TOKEN)
    again = state_store.load_or_create_pending_replacement(state_path, CONTROLLER, TOKEN)
    journal = state_store.replacement_pending_path(state_path)
    raw = json.loads(journal.read_text())
    assert first == again == state_store.load_pending_replacement(state_path, CONTROLLER, TOKEN)
    assert raw == {
        "version": 1,
        "request_id": first.request_id,
        "proposed_agent_secret": first.proposed_agent_secret,
        "controller_url": CONTROLLER,
        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
    }
    assert TOKEN not in journal.read_text()
    assert TOKEN not in repr(first) and first.proposed_agent_secret not in repr(first)
    if os.name != "nt":
        assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert journal.stat().st_uid == os.geteuid()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "controller,token",
    [(CONTROLLER, OTHER_TOKEN), ("https://other.example", TOKEN)],
)
def test_bound_journal_rejects_changed_token_or_controller_without_mutation(
    tmp_path, controller, token,
):
    state_path = tmp_path / "state.json"
    state_store.load_or_create_pending_replacement(state_path, CONTROLLER, TOKEN)
    journal = state_store.replacement_pending_path(state_path)
    original = journal.read_bytes()
    with pytest.raises(StateError, match="bindings"):
        state_store.load_pending_replacement(state_path, controller, token)
    with pytest.raises(StateError, match="bindings"):
        state_store.load_or_create_pending_replacement(state_path, controller, token)
    assert journal.read_bytes() == original


@pytest.mark.parametrize("kind", ["symlink", "directory", "public_mode"])
def test_unsafe_replacement_journal_is_preserved_and_rejected(tmp_path, kind):
    state_path = tmp_path / "state.json"
    journal = state_store.replacement_pending_path(state_path)
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("unrelated private data")
        try:
            journal.symlink_to(target)
        except OSError as exc:
            if exc.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP} or getattr(exc, "winerror", None) == 1314:
                pytest.skip(f"symlinks are unavailable: {exc}")
            raise
    elif kind == "directory":
        journal.mkdir()
    else:
        if os.name == "nt":
            pytest.skip("POSIX mode checks")
        state_store.load_or_create_pending_replacement(state_path, CONTROLLER, TOKEN)
        journal.chmod(0o644)
    with pytest.raises(StateError, match="private and regular"):
        state_store.load_pending_replacement(state_path, CONTROLLER, TOKEN)
    with pytest.raises(StateError, match="private and regular"):
        state_store.load_or_create_pending_replacement(state_path, CONTROLLER, TOKEN)
    assert journal.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync ordering")
def test_replacement_journal_publishes_only_after_file_fsync(tmp_path, monkeypatch):
    events = []
    real_fsync, real_link = os.fsync, os.link

    def fsync(descriptor):
        info = os.fstat(descriptor)
        events.append("file_fsync" if stat.S_ISREG(info.st_mode) else "directory_fsync")
        real_fsync(descriptor)

    def link(source, destination):
        assert events == ["file_fsync"]
        assert not Path(destination).exists()
        events.append("publish")
        real_link(source, destination)

    monkeypatch.setattr(state_store.os, "fsync", fsync)
    monkeypatch.setattr(state_store.os, "link", link)
    state_store.load_or_create_pending_replacement(tmp_path / "state.json", CONTROLLER, TOKEN)
    assert events == ["file_fsync", "publish", "directory_fsync"]


def test_legacy_unbound_journal_requires_manual_review_without_deletion(runtime_factory):
    config, new = runtime_factory
    journal = state_store.replacement_pending_path(config.state_file)
    legacy = {
        "request_id": uuid.uuid4().hex,
        "proposed_agent_secret": "rpa_" + "A" * 48,
    }
    journal.write_text(json.dumps(legacy))
    if os.name != "nt":
        journal.chmod(0o600)
    original = journal.read_bytes()
    client = RecordingClient()
    with pytest.raises(StateError, match="legacy replacement journal.*manual review"):
        new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
    assert journal.read_bytes() == original
    assert client.calls == [] and client.enrollment_calls == []
    assert not config.state_file.exists()
    # Once the primary identity is durable, an old stale journal may be removed.
    state_store.save_state(config.state_file, AgentState.new(AGENT_ID, "durable-secret"), 20)
    assert new(client=client).ensure_enrolled(cli_replacement_token=TOKEN).agent_id == AGENT_ID
    assert not journal.exists()
    assert client.calls == [] and client.enrollment_calls == []


def test_lost_replacement_response_retries_same_identity_only_with_original_token(
    runtime_factory,
):
    config, new = runtime_factory
    client = RecordingClient(lose_first_response=True)
    with pytest.raises(RuntimeError, match="synthetic response loss"):
        new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
    journal = state_store.replacement_pending_path(config.state_file)
    raw = journal.read_bytes()
    with pytest.raises(StateError, match="bindings"):
        new(client=client).ensure_enrolled(cli_replacement_token=OTHER_TOKEN)
    assert journal.read_bytes() == raw
    assert len(client.calls) == 1
    recovered = new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
    assert len(client.calls) == 2 and client.calls[0] == client.calls[1]
    assert state_store.load_state(config.state_file).agent_secret == recovered.agent_secret
    assert not journal.exists()
    assert new(client=client).ensure_enrolled(cli_replacement_token=OTHER_TOKEN).agent_id == AGENT_ID
    assert len(client.calls) == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync ordering")
def test_visible_replacement_state_is_durable_before_journal_cleanup(
    runtime_factory, monkeypatch,
):
    config, new = runtime_factory
    client = RecordingClient()
    journal = state_store.replacement_pending_path(config.state_file)
    original_sync = state_store._fsync_directory

    def fail_after_primary_replace(path):
        if config.state_file.exists():
            raise OSError("synthetic primary directory fsync failure")
        original_sync(path)

    with monkeypatch.context() as patched:
        patched.setattr(state_store, "_fsync_directory", fail_after_primary_replace)
        with pytest.raises(StateError):
            new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
        visible = state_store.load_state(config.state_file)
        published = journal.read_bytes()
        recovered = new(client=client).ensure_enrolled()
        assert recovered.agent_secret == visible.agent_secret
        assert journal.read_bytes() == published
        assert len(client.calls) == 1

    recovered = new(client=client).ensure_enrolled()
    assert recovered.agent_secret == visible.agent_secret
    assert not journal.exists()
    assert len(client.calls) == 1 and client.enrollment_calls == []


def test_replacement_pending_blocks_ordinary_and_tokenless_registration(
    runtime_factory,
):
    config, new = runtime_factory
    state_store.load_or_create_pending_replacement(config.state_file, CONTROLLER, TOKEN)
    client = RecordingClient()
    with pytest.raises(RuntimeError, match="original inheritance token"):
        new(client=client).ensure_enrolled(cli_token="rpe_" + "E" * 43)
    with pytest.raises(RuntimeError, match="original inheritance token"):
        new(client=client).ensure_enrolled()
    assert client.calls == [] and client.enrollment_calls == []
    assert not state_store.enrollment_pending_path(config.state_file).exists()


def test_old_config_token_is_ignored_for_explicit_inheritance_but_explicit_tokens_conflict(
    runtime_factory, monkeypatch,
):
    config, new = runtime_factory
    inherited_config = replace(config, enrollment_token="rpe_" + "C" * 43)
    client = RecordingClient()
    with pytest.raises(RuntimeError, match="cannot be used together"):
        new(inherited_config, client).ensure_enrolled(
            cli_token="rpe_" + "E" * 43, cli_replacement_token=TOKEN,
        )
    monkeypatch.setenv("RIGPILOT_ENROLLMENT_TOKEN", "rpe_" + "F" * 43)
    with pytest.raises(RuntimeError, match="cannot be used together"):
        new(inherited_config, client).ensure_enrolled(cli_replacement_token=TOKEN)
    monkeypatch.delenv("RIGPILOT_ENROLLMENT_TOKEN")
    inherited = new(inherited_config, client).ensure_enrolled(cli_replacement_token=TOKEN)
    assert inherited.agent_id == AGENT_ID
    assert len(client.calls) == 1 and client.enrollment_calls == []


def test_both_pending_modes_conflict_before_any_post(runtime_factory):
    config, new = runtime_factory
    state_store.load_or_create_pending_replacement(config.state_file, CONTROLLER, TOKEN)
    state_store.load_or_create_pending_enrollment(
        config.state_file, CONTROLLER, "rpe_" + "E" * 43, METADATA,
    )
    client = RecordingClient()
    with pytest.raises(RuntimeError, match="both pending"):
        new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
    assert client.calls == [] and client.enrollment_calls == []
    assert state_store.replacement_pending_path(config.state_file).exists()
    assert state_store.enrollment_pending_path(config.state_file).exists()


def test_changed_controller_or_non_durable_journal_never_sends_replacement(
    runtime_factory, monkeypatch,
):
    config, new = runtime_factory
    state_store.load_or_create_pending_replacement(config.state_file, CONTROLLER, TOKEN)
    client = RecordingClient()
    with pytest.raises(StateError, match="bindings"):
        new(replace(config, server_url="https://other.example"), client).ensure_enrolled(
            cli_replacement_token=TOKEN
        )
    assert client.calls == []
    def fail_fsync(_directory):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(state_store, "_fsync_directory", fail_fsync)
    with pytest.raises(StateError, match="durable"):
        new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
    assert client.calls == []


def test_mismatched_controller_reply_never_saves_primary_state(runtime_factory):
    config, new = runtime_factory
    client = RecordingClient(wrong_secret=True)
    with pytest.raises(ProtocolError, match="replacement credentials"):
        new(client=client).ensure_enrolled(cli_replacement_token=TOKEN)
    assert len(client.calls) == 1
    assert not config.state_file.exists()
    assert state_store.replacement_pending_path(config.state_file).exists()


@pytest.mark.parametrize(
    "bad_token",
    ["short", "rpr_" + "A" * 42, "rpr_" + "A" * 44, "rpr_" + "!" * 43],
)
def test_malformed_replacement_code_never_creates_journal_or_posts(
    runtime_factory, bad_token,
):
    config, new = runtime_factory
    client = RecordingClient()
    with pytest.raises(StateError, match="replacement token must"):
        new(client=client).ensure_enrolled(cli_replacement_token=bad_token)
    assert not state_store.replacement_pending_path(config.state_file).exists()
    assert not config.state_file.exists()
    assert client.calls == [] and client.enrollment_calls == []
