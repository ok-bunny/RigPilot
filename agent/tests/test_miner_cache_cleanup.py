"""Isolated cache cleanup gates; never touch a real Agent data directory."""
import errno
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from unittest import mock
from pathlib import Path

import pytest

from agent.config import AgentConfig
from agent import miner_cache_cleanup as cache
REAL_PROCESS_REFS = cache._process_refs
from agent.commands import Command, CommandOutcome
from agent.runtime import AgentRuntime
from agent.state import AgentState, StateError
from agent import miner_packages
from agent.commands import CommandExecutor
from agent.commands import CommandRejected
from agent.managed_miners import ManagedMinerManager


@pytest.fixture(autouse=True)
def isolated_process_table(monkeypatch):
    # Tests must not depend on the host machine's inaccessible system PIDs.
    monkeypatch.setattr(cache, "_process_refs", lambda _root, _names, _deadline: (set(), False))
    if os.name == "nt":
        # The production ACL gate is independently exercised by Windows
        # installer/update tests; this fixture is created by pytest under a
        # disposable user-owned directory rather than protected ProgramData.
        monkeypatch.setattr("agent.agent_updates._windows_acl_is_update_safe", lambda _path: True)


def fixture(tmp_path):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    root = data / "packages"
    root.mkdir(mode=0o700)
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=data / "state.json",
        flight_sheet_file=data / "flight-sheet.json",
        managed_mining_enabled=True,
        managed_miner_runtime_dir=data / "miner-runtime",
        miner_packages_dir=root,
    )
    return config, root


def package(root, name="xmrig-official", version="1.0", *, body=b"miner", side=False):
    sha = "a" * 64
    marker = {"id": name, "name": "XMRig", "version": version,
              "archive_type": "zip", "sha256": sha, "file_count": 1,
              "extracted_bytes": len(body), "tree_sha256": "b" * 64,
              "verified_archive_sha256_v1": sha}
    target = cache._verified_cache_target(root, marker, sha) if side else root / f"{name}-{version}-{sha[:12]}"
    target.mkdir(mode=0o700)
    (target / "miner.exe").write_bytes(body)
    (target / ".rigpilot-package.json").write_text(json.dumps(marker), encoding="utf-8")
    return target, marker


def flight(config, *targets):
    legs = []
    for target in targets:
        legs.append({"miner_package": {"id": "xmrig-official", "version": target.name.split("-")[2],
                                       "installed_path": str(target)}})
    config.flight_sheet_file.write_text(json.dumps({"primary": legs[0] if legs else None,
        "secondary": legs[1] if len(legs) > 1 else None}), encoding="utf-8")


def test_scan_and_cleanup_obsolete_cache_preserves_current_primary_secondary(tmp_path):
    config, root = fixture(tmp_path)
    current, _ = package(root, version="1.0")
    secondary, _ = package(root, version="2.0")
    old, _ = package(root, version="0.9")
    flight(config, current, secondary)
    found = cache.scan(config)
    assert found["can_cleanup"] is True
    assert [row["version"] for row in found["candidates"]] == ["0.9"]
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "succeeded"
    assert result["logical_bytes_removed"] == len(b"miner")
    assert not old.exists() and current.exists() and secondary.exists()
    empty = cache.scan(config)
    assert empty["can_cleanup"] is True and empty["candidates"] == []


def test_verified_side_slot_is_a_cache_not_an_unknown_stage(tmp_path):
    config, root = fixture(tmp_path)
    side, _ = package(root, side=True)
    found = cache.scan(config)
    assert found["can_cleanup"] is True
    assert len(found["candidates"]) == 1
    assert cache.cleanup(config, found["scan_fingerprint"])["state"] == "succeeded"
    assert not side.exists()


def test_cleanup_total_deadline_bounds_fresh_reference_scan_without_moving_candidate(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    found = cache.scan(config)
    clock = {"now": 0.0}
    deadlines = []
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cache, "_MAX_CLEANUP_SECONDS", 1.0)
    def process_refs(_root, _names, deadline):
        deadlines.append(deadline)
        if len(deadlines) == 2:
            clock["now"] = 2.0  # The final fresh observation uses up the budget.
        return set(), False
    monkeypatch.setattr(cache, "_process_refs", process_refs)
    replace = mock.Mock(side_effect=AssertionError("expired cleanup must not rename"))
    remove = mock.Mock(side_effect=AssertionError("expired cleanup must not delete"))
    monkeypatch.setattr(cache.os, "replace", replace)
    monkeypatch.setattr(cache.shutil, "rmtree", remove)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert deadlines == [1.0, 1.0]
    assert result["state"] == "partial" and result["reason"] == "cleanup_timeout"
    assert result["logical_bytes_removed"] == 0 and result["deleted"] == []
    assert candidate.exists() and (candidate / "miner.exe").read_bytes() == b"miner"
    assert not list(root.glob(".rigpilot-cleanup-*"))
    replace.assert_not_called()
    remove.assert_not_called()


@pytest.mark.parametrize("stage", ["marker", "quarantine-tree"])
def test_cleanup_rechecks_total_deadline_before_rename_and_delete(tmp_path, monkeypatch, stage):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    found = cache.scan(config)
    clock = {"now": 0.0, "calls": 0}
    monkeypatch.setattr(cache.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(cache, "_MAX_CLEANUP_SECONDS", 1.0)
    helper_name = "_marker" if stage == "marker" else "_tree"
    original = getattr(cache, helper_name)
    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock["calls"] += 1
        if clock["calls"] == 3:  # Initial scan, fresh scan, then mutation preflight.
            clock["now"] = 2.0
        return result
    monkeypatch.setattr(cache, helper_name, observed)
    remove = mock.Mock(side_effect=AssertionError("expired cleanup must not delete"))
    monkeypatch.setattr(cache.shutil, "rmtree", remove)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "partial" and result["reason"] == "cleanup_timeout"
    assert result["logical_bytes_removed"] == 0 and result["deleted"] == []
    retained = candidate if stage == "marker" else next(root.glob(".rigpilot-cleanup-*"))
    assert retained.exists() and (retained / "miner.exe").read_bytes() == b"miner"
    assert candidate.exists() is (stage == "marker")
    remove.assert_not_called()


def test_retained_failed_cleanup_does_not_block_unrelated_verified_package(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    first, _ = package(root, version="0.8")
    second, _ = package(root, version="0.9")
    found = cache.scan(config)
    real_remove = cache.shutil.rmtree
    def failed_remove(path):
        raise PermissionError("owned cleanup failure")
    monkeypatch.setattr(cache.shutil, "rmtree", failed_remove)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "partial" and not first.exists() and second.exists()
    residue = next(root.glob(".rigpilot-cleanup-*"))
    saved = {p.name: p.read_bytes() for p in residue.iterdir()}
    monkeypatch.setattr(cache.shutil, "rmtree", real_remove)
    next_scan = cache.scan(config)
    assert next_scan["can_cleanup"] is True and next_scan["reason"] is None
    assert next_scan["unclassified"] == [{"kind": "package_cache", "entry_id": residue.name,
                                            "reason": "cleanup_residue_retained"}]
    assert cache.cleanup(config, next_scan["scan_fingerprint"])["state"] == "succeeded"
    assert not second.exists() and {p.name: p.read_bytes() for p in residue.iterdir()} == saved


def test_active_install_stage_still_blocks_cleanup_with_retained_residue(tmp_path):
    config, root = fixture(tmp_path)
    target, _ = package(root)
    (root / (".rigpilot-cleanup-" + "a" * 64)).mkdir()
    (root / ".xmrig-official.installing").mkdir()
    found = cache.scan(config)
    assert found["can_cleanup"] is False and found["reason"] == "installation_in_progress"
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "failed" and target.exists()


def test_nested_marker_named_payload_is_counted_like_the_sealed_installer(tmp_path):
    from dataclasses import replace
    from agent.tests.test_miner_packages import metadata, requester_for, zip_bytes

    config, _root = fixture(tmp_path)
    config = replace(config, allow_miner_downloads=True,
                     miner_download_host_allowlist=frozenset({"downloads.example.test"}))
    body = zip_bytes({"miner.exe": b"miner", "resources/.rigpilot-package.json": b"payload"})
    declared = miner_packages.validate_package_metadata(metadata(body), config)
    requester, _response, _calls = requester_for(body)
    installed = miner_packages.install_miner_package(declared, config, requester=requester)
    target = Path(installed["path"])
    marker = miner_packages._verify_installed_package(target, declared, 1024 * 1024)
    found = cache.scan(config)
    assert found["unclassified"] == []
    assert found["can_cleanup"] is True
    assert found["candidates"][0]["file_count"] == marker["file_count"] == 2
    assert found["candidates"][0]["logical_bytes"] == marker["extracted_bytes"] == 12
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "succeeded" and not target.exists()
    assert result["logical_bytes_removed"] == 12


def test_scan_then_flight_switch_refuses_old_fingerprint(tmp_path):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    found = cache.scan(config)
    flight(config, candidate)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "stale_scan"
    assert candidate.exists()


def test_pending_or_independent_role_flight_protects_stopped_package(tmp_path):
    config, root = fixture(tmp_path)
    pending, _ = package(root, version="3.0")
    role, _ = package(root, version="4.0")
    pending_path = config.flight_sheet_file.with_name("." + config.flight_sheet_file.name + ".pending")
    pending_path.write_text(json.dumps({"primary": {"miner_package": {"id": "xmrig-official",
        "version": "3.0", "installed_path": str(pending)}}, "secondary": None}), encoding="utf-8")
    role_path = config.managed_miner_runtime_dir / "secondary/active-flight-sheet.json"
    role_path.parent.mkdir(parents=True)
    role_path.write_text(json.dumps({"primary": {"miner_package": {"id": "xmrig-official",
        "version": "4.0", "installed_path": str(role)}}, "secondary": None}), encoding="utf-8")
    found = cache.scan(config)
    assert found["can_cleanup"] is True and found["candidates"] == []
    assert {row["version"] for row in found["protected"]} == {"3.0", "4.0"}


def test_transition_journal_blocks_all_cache_cleanup(tmp_path):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    runtime = config.managed_miner_runtime_dir
    runtime.mkdir()
    (runtime / "flight-transition.json").write_text("{}", encoding="utf-8")
    found = cache.scan(config)
    assert found["can_cleanup"] is False and found["reason"] == "active_session"
    assert candidate.exists()


def test_normal_independent_dual_manifest_does_not_block_third_old_package(tmp_path):
    config, root = fixture(tmp_path)
    first, _ = package(root, version="1.0")
    second, _ = package(root, version="2.0")
    old, _ = package(root, version="0.9")
    runtime = config.managed_miner_runtime_dir
    runtime.mkdir()
    (runtime / "independent-miners.json").write_text(json.dumps({
        "version": 1, "mode": "independent", "flight_sheet_id": "flight",
        "roles": ["primary", "secondary"]}), encoding="utf-8")
    for role, target in (("primary", first), ("secondary", second)):
        slot = runtime / role
        slot.mkdir()
        (slot / "active-flight-sheet.json").write_text(json.dumps({"primary": {
            "miner_package": {"id": "xmrig-official", "version": target.name.split("-")[2],
                "installed_path": str(target)}}, "secondary": None}), encoding="utf-8")
    found = cache.scan(config)
    assert found["can_cleanup"] is True
    assert [row["version"] for row in found["candidates"]] == ["0.9"]
    assert {row["version"] for row in found["protected"]} == {"1.0", "2.0"}
    assert cache.cleanup(config, found["scan_fingerprint"])["state"] == "succeeded"
    assert first.exists() and second.exists() and not old.exists()


def test_multiple_candidates_and_repeated_cleanup_are_bounded(tmp_path):
    config, root = fixture(tmp_path)
    first, _ = package(root, version="1.0")
    second, _ = package(root, version="2.0")
    found = cache.scan(config)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "succeeded" and len(result["deleted"]) == 2
    assert not first.exists() and not second.exists()
    repeat = cache.cleanup(config, found["scan_fingerprint"])
    assert repeat["state"] == "stale_scan" and repeat["deleted"] == []


def test_running_install_lock_prevents_cleanup(tmp_path):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    found = cache.scan(config)
    entered = threading.Event()
    release = threading.Event()
    def installer():
        with cache.cache_operation_lock(root):
            entered.set()
            release.wait(2)
    thread = threading.Thread(target=installer)
    thread.start()
    assert entered.wait(1)
    try:
        # Time-bound local contention instead of waiting through a package download.
        with pytest.raises(cache.MinerCacheCleanupError, match="cache_busy"):
            with cache.cache_operation_lock(root, wait_seconds=0.01):
                pass
        assert candidate.exists()
    finally:
        release.set()
        thread.join(timeout=2)


def test_child_count_limit_never_yields_truncated_success(tmp_path):
    config, root = fixture(tmp_path)
    for index in range(cache._MAX_CHILDREN + 1):
        (root / f"unknown-{index}").mkdir()
    found = cache.scan(config)
    assert found["can_cleanup"] is False
    assert found["scan_fingerprint"] is None
    assert found["reason"] == "scan_limit_exceeded"


def test_tree_entry_limit_is_global_failure_not_completed_unclassified(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    target, _ = package(root)
    (target / "other").write_bytes(b"x")
    monkeypatch.setattr(cache, "_MAX_TREE_ENTRIES", 1)
    found = cache.scan(config)
    assert found["scan_fingerprint"] is None
    assert found["can_cleanup"] is False and found["reason"] == "scan_limit_exceeded"


def test_install_and_managed_start_use_same_cache_lock(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    calls = []
    @contextmanager
    def spy(path, *, wait_seconds=3.0):
        assert path == root
        calls.append("lock")
        yield
        calls.append("unlock")
    monkeypatch.setattr(cache, "cache_operation_lock", spy)
    monkeypatch.setattr(miner_packages, "_install_miner_package_unlocked",
        lambda *_args, **_kwargs: {"path": "fixture"})
    assert miner_packages.install_miner_package({}, config) == {"path": "fixture"}
    manager = ManagedMinerManager(config)
    monkeypatch.setattr(manager, "_apply_unlocked", lambda _document: {"started": True})
    assert manager.apply({}) == {"started": True}
    executor = CommandExecutor(config)
    monkeypatch.setattr(executor, "_execute_authorized", lambda *_args, **_kwargs:
        CommandOutcome(status="succeeded", result={}))
    assert executor.execute(Command("id", "apply_flight_sheet", {})).status == "succeeded"
    assert calls == ["lock", "unlock"] * 3


def test_command_payload_contract_and_two_phase_result(tmp_path):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    executor = CommandExecutor(config)
    with pytest.raises(CommandRejected):
        executor.authorize(Command("bad", cache.CLEANUP_ACTION, {"path": str(candidate)}))
    with pytest.raises(CommandRejected):
        executor.authorize(Command("bad", cache.CLEANUP_ACTION, {"scan_fingerprint": "A" * 64}))
    scanned = executor.execute(Command("scan", cache.SCAN_ACTION, {}))
    assert scanned.status == "succeeded" and scanned.result["can_cleanup"] is True
    assert len(scanned.result["candidates"]) == 1
    cleaned = executor.execute(Command("clean", cache.CLEANUP_ACTION,
        {"scan_fingerprint": scanned.result["scan_fingerprint"]}))
    assert cleaned.status == "succeeded"
    assert cleaned.result["state"] == "succeeded" and not candidate.exists()


def test_link_and_external_sentinel_are_never_deleted(tmp_path):
    config, root = fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("untouched")
    candidate, _ = package(root)
    link = candidate / "external-link"
    if os.name == "nt":
        # Non-elevated Windows accounts may lack SeCreateSymbolicLinkPrivilege;
        # a directory junction still exercises the real NTFS reparse gate.
        created = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(external)],
            capture_output=True, text=True, check=False)
        if created.returncode != 0:
            pytest.skip("Windows test user cannot create a local junction")
    else:
        link.symlink_to(external, target_is_directory=True)
    found = cache.scan(config)
    assert found["candidates"] == []
    assert found["unclassified"][0]["reason"] == "unsafe_link"
    assert sentinel.read_text() == "untouched"


def test_hardlinked_external_file_is_not_a_candidate(tmp_path):
    config, root = fixture(tmp_path)
    external = tmp_path / "outside-data"
    external.write_bytes(b"untouched")
    candidate, marker = package(root, body=b"untouched")
    (candidate / "miner.exe").unlink()
    try:
        (candidate / "miner.exe").hardlink_to(external)
    except OSError:
        pytest.skip("filesystem does not support fixture hardlinks")
    (candidate / ".rigpilot-package.json").write_text(json.dumps(marker), encoding="utf-8")
    found = cache.scan(config)
    assert found["candidates"] == []
    assert found["unclassified"][0]["reason"] == "unsafe_entry"
    assert external.read_bytes() == b"untouched"


def test_reparse_like_swap_before_unlink_keeps_external_sentinel(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    external = tmp_path / "outside"
    external.mkdir()
    sentinel = external / "keep"
    sentinel.write_text("untouched")
    candidate, _ = package(root)
    found = cache.scan(config)
    real_replace = cache.os.replace
    def swap(source, destination):
        # This models a writer replacing the source with a directory link in
        # the final narrow window after the repeated reference scan.
        if source == candidate:
            moved = root / "original-retained"
            real_replace(source, moved)
            source.symlink_to(external, target_is_directory=True)
        return real_replace(source, destination)
    monkeypatch.setattr(cache.os, "replace", swap)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "partial"
    assert result["logical_bytes_removed"] == 0
    assert sentinel.read_text() == "untouched"


def test_read_only_scan_on_enospc_and_cleanup_fails_closed(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    found = cache.scan(config)
    real_open = cache.os.open
    def full(path, flags, *args, **kwargs):
        if str(path).endswith(cache._LOCK_NAME):
            raise OSError(errno.ENOSPC, "full")
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(cache.os, "open", full)
    assert cache.scan(config)["scan_fingerprint"] == found["scan_fingerprint"]
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "failed" and candidate.exists()


def test_partial_delete_never_reports_full_success(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    candidate, _ = package(root)
    package(root, version="2.0")
    found = cache.scan(config)
    def denied(_path):
        raise PermissionError("denied")
    monkeypatch.setattr(cache.shutil, "rmtree", denied)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "partial"
    assert result["logical_bytes_removed"] == 0
    assert result["failed"][0]["reason"] == "permission_denied"
    assert len(result["skipped"]) == 1 and result["skipped"][0]["reason"] == "not_attempted"


def test_read_only_scan_can_report_when_state_history_hits_enospc(tmp_path, monkeypatch):
    config, _root = fixture(tmp_path)
    runtime = object.__new__(AgentRuntime)
    runtime.config = config
    state = AgentState.new("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret")
    command = Command("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff", cache.SCAN_ACTION, {})
    outcome = CommandOutcome(status="succeeded", result={"scan_fingerprint": "a" * 64})
    def full(*_args, **_kwargs):
        try:
            raise OSError(errno.ENOSPC, "full")
        except OSError as exc:
            raise StateError("could not save state") from exc
    save = mock.Mock(side_effect=full)
    monkeypatch.setattr("agent.runtime.save_state", save)
    response = runtime._remember_outcome(state, command, outcome)
    assert response["status"] == "succeeded"
    assert command.command_id not in state.command_history
    save.assert_not_called()
    with pytest.raises(StateError):
        runtime._remember_outcome(state,
            Command(command.command_id, cache.CLEANUP_ACTION, {"scan_fingerprint": "a" * 64}),
            outcome)
    save.assert_called_once()


def test_enospc_scan_result_reaches_controller_without_persisted_history(tmp_path, monkeypatch):
    config, _root = fixture(tmp_path)
    runtime = AgentRuntime(config)
    responses = []
    runtime.client = mock.Mock(post_result=lambda _state, _command_id, value:
        responses.append(value) or value["status"])
    runtime.executor = mock.Mock(execute=lambda *_args, **_kwargs:
        CommandOutcome(status="succeeded", result={"scan_fingerprint": "a" * 64}))
    state = AgentState.new("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret")
    def full(*_args, **_kwargs):
        try:
            raise OSError(errno.ENOSPC, "full")
        except OSError as exc:
            raise StateError("could not save state") from exc
    monkeypatch.setattr("agent.runtime.save_state", full)
    runtime._handle_command(state, {"id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
        "action": cache.SCAN_ACTION, "payload": {}})
    assert [response["status"] for response in responses] == ["running", "succeeded"]
    assert state.command_history == {}


def test_disk_result_reports_only_the_package_cache_filesystem(tmp_path, monkeypatch):
    config, root = fixture(tmp_path)
    package(root)
    runtime = config.managed_miner_runtime_dir
    runtime.mkdir()
    real_stat = Path.stat
    cache_device = real_stat(root).st_dev
    def separate_runtime_device(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == runtime:
            fields = list(info)
            fields[2] = cache_device + 1
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(Path, "stat", separate_runtime_device)
    found = cache.scan(config)
    rows = found["disk"]["filesystems"]
    assert found["disk"]["scope"] == "package_cache_filesystem"
    assert {row["scope"] for row in rows} == {"package_cache"}
    assert len({row["filesystem_key"] for row in rows}) == 1
    cleaned = cache.cleanup(config, found["scan_fingerprint"])
    assert cleaned["state"] == "succeeded"
    assert len(cleaned["disk"]["filesystems"]) == 1
    assert cleaned["disk"]["scope"] == "package_cache_filesystem"


def test_scan_report_reuses_one_disk_usage_sample_when_free_bytes_drift(tmp_path, monkeypatch):
    # Failure injection for the 2026-10-27 macOS incident form: APFS
    # purgeable space drifts between consecutive disk_usage calls on one
    # volume (observed +431 MiB free within minutes).  The controller
    # compares each package_cache row with the top-level disk fields
    # byte-for-byte, so a drifting sampler must be taken exactly once per
    # report and reused for both fields.
    config, root = fixture(tmp_path)
    package(root)
    samples = []

    def drifting(_path):
        samples.append(1)
        return mock.Mock(total=10**12, used=10**11,
                         free=552_373_944_320 + 452_984_832 * len(samples))

    monkeypatch.setattr(cache.shutil, "disk_usage", drifting)
    found = cache.scan(config)
    assert found["disk"]["filesystems"], found["disk"]
    row = found["disk"]["filesystems"][0]
    assert row["total_bytes"] == found["disk"]["total_bytes"]
    assert row["free_bytes"] == found["disk"]["free_bytes"]
    assert len(samples) == 1


def test_cleanup_report_after_phase_reuses_one_disk_usage_sample(tmp_path, monkeypatch):
    # The cleanup result faces the same controller byte-for-byte equality
    # between the package_cache row and the top-level disk fields; the
    # after phase must not take a second, already-drifted sample.
    config, root = fixture(tmp_path)
    package(root)
    found = cache.scan(config)
    samples = []

    def drifting(_path):
        samples.append(1)
        return mock.Mock(total=10**12, used=10**11,
                         free=552_373_944_320 + 452_984_832 * len(samples))

    monkeypatch.setattr(cache.shutil, "disk_usage", drifting)
    result = cache.cleanup(config, found["scan_fingerprint"])
    assert result["state"] == "succeeded", result
    assert result["disk"]["filesystems"], result["disk"]
    row = result["disk"]["filesystems"][0]
    assert row["free_bytes_before"] == result["disk"]["free_bytes_before"]
    assert row["free_bytes_after"] == result["disk"]["free_bytes_after"]
    assert row["free_bytes_delta"] == result["disk"]["free_bytes_delta"]


@pytest.mark.skipif(os.name != "posix" or not Path("/proc/self/exe").exists(),
                    reason="Requires a Linux /proc process view")
def test_linux_root_process_executable_keeps_its_cache_leaf(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("Requires the native Linux root target gate")
    import shutil
    import time
    config, root = fixture(tmp_path)
    target, marker = package(root)
    executable = target / "sleep"
    shutil.copyfile("/bin/sleep", executable)
    executable.chmod(0o700)
    (target / "miner.exe").unlink()
    marker["file_count"] = 1
    marker["extracted_bytes"] = executable.stat().st_size
    (target / ".rigpilot-package.json").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(cache, "_process_refs", REAL_PROCESS_REFS)
    process = subprocess.Popen([str(executable), "10"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        seen = None
        for _ in range(30):
            seen = cache.scan(config)
            if seen["protected"] or seen["reason"] == "unknown_reference":
                break
            time.sleep(0.05)
        assert seen is not None and seen["candidates"] == [], seen
        import psutil
        reported_executable = psutil.Process(process.pid).exe()
        if cache._within(Path(reported_executable), root) is None:
            # ARM/Rosetta reports its interpreter, not the copied ELF, as
            # /proc/PID/exe. The policy must fail closed instead of deleting.
            assert seen["can_cleanup"] is False and seen["reason"] == "unknown_reference"
        else:
            assert seen["can_cleanup"] is True
            assert seen["protected"][0]["reason"] == "active_process"
    finally:
        process.terminate()
        process.wait(timeout=5)
    after = cache.scan(config)
    assert after["can_cleanup"] is True and len(after["candidates"]) == 1
