from __future__ import annotations

import json
import os
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import AgentConfig
from agent.miner_packages import _MARKER_NAME, _tree_fingerprint, _verify_installed_package
import agent.native_miner_runtime as native


@pytest.fixture
def sealed(tmp_path):
    packages = tmp_path / "packages"
    source = packages / "srbminer-3.7.1"
    source.mkdir(parents=True, mode=0o700)
    source.chmod(0o700)
    (source / "SRBMiner-MULTI").write_bytes(b"sealed executable")
    (source / "SRBMiner-MULTI").chmod(0o700)
    (source / "libraries").mkdir(mode=0o700)
    (source / "libraries/libexample.so").write_bytes(b"sealed library")
    (source / "empty").mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    metadata = {"id": "srbminer", "name": "SRBMiner", "version": "3.7.1",
                "download_url": "https://controller.example.test/package",
                "archive_type": "tar.gz", "sha256": "a" * 64}
    baseline = _tree_fingerprint(source, 1024**2)
    marker = {**metadata, **baseline, "verified_archive_sha256_v1": metadata["sha256"]}
    (source / _MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    config = AgentConfig(server_url="https://controller.example.test", state_file=tmp_path / "state.json",
                         miner_packages_dir=packages, managed_miner_runtime_dir=runtime,
                         miner_download_max_mb=1)
    return source, metadata, config, runtime


def prepare(sealed, role="single"):
    source, metadata, config, runtime = sealed
    if role != "single":
        runtime = runtime / role
        runtime.mkdir(mode=0o700, exist_ok=True)
    return native.prepare_native_srb_bundle(source, metadata, config, runtime, role=role)


def tree_bytes(root):
    """Capture caller-owned fixture contents without following links."""
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = ("link", os.readlink(path))
        elif path.is_file():
            result[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            result[relative] = ("directory",)
    return result


def test_new_generation_sibling_preserves_actual031_namespace_parser(sealed):
    _, _, config, _ = sealed
    old = runpy.run_path(str(Path(__file__).parent / "fixtures/legacy031_native_namespace.py"))
    legacy = config.miner_packages_dir / native.LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY
    for role in old["_ROLES"]:
        for name in old["_SLOTS"]:
            slot = legacy / role / name
            slot.mkdir(parents=True, mode=0o700)
            (slot / "keep").write_bytes(b"existing031slot")
    before = tree_bytes(legacy)
    bundle = prepare(sealed)
    old["_children"](legacy, set(old["_ROLES"]))
    for role in old["_ROLES"]:
        old["_children"](legacy / role, {*old["_SLOTS"], old["_RECLAIM"]})
    assert bundle.is_relative_to(config.miner_packages_dir / native.NATIVE_SRB_RUNTIME_DIRECTORY)
    assert not bundle.is_relative_to(legacy)
    assert tree_bytes(legacy) == before


@pytest.mark.parametrize("pending", [False, True])
def test_existing034_generation_is_retained_without_closed_proof_in_legacy_root(sealed, pending):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    role_root = config.miner_packages_dir / native.LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY / "single"
    role_root.mkdir(parents=True, mode=0o700)
    bundle.parent.rename(role_root / bundle.parent.name)
    old_bundle = role_root / bundle.parent.name / "bundle"
    control = old_bundle.parent / "control.json"
    record = json.loads(control.read_text())
    record["owner"] = "f" * 32 if native._OWNER != "f" * 32 else "e" * 32
    control.write_text(json.dumps(record))
    if pending:
        (old_bundle.parent / "probe-pending").touch(mode=0o600)
    before = tree_bytes(role_root)
    assert native.discard_native_srb_bundle(old_bundle, config, runtime, role="single") is False
    newer = prepare(sealed)
    assert newer.is_relative_to(config.miner_packages_dir / native.NATIVE_SRB_RUNTIME_DIRECTORY)
    assert tree_bytes(role_root) == before
    if pending:
        assert native.discard_native_srb_bundle(old_bundle, config, runtime, role="single", confirmed_stop=True) is False
        assert tree_bytes(role_root) == before
    else:
        # The test-owned directory was never exposed to a process; the caller
        # now explicitly supplies positive closed evidence for this exact run.
        assert native.discard_native_srb_bundle(old_bundle, config, runtime, role="single", confirmed_stop=True) is True
        assert not old_bundle.parent.exists()


@pytest.mark.parametrize("directory", [native.LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY, native.NATIVE_SRB_RUNTIME_DIRECTORY])
def test_runtime_namespaces_cannot_be_used_as_sealed_baselines(sealed, directory):
    source, metadata, config, runtime = sealed
    container = config.miner_packages_dir / directory
    container.mkdir(mode=0o700)
    source.rename(container / "looks-sealed")
    with pytest.raises(native.NativeMinerRuntimeError, match="sealed package cache"):
        native.prepare_native_srb_bundle(container / "looks-sealed", metadata, config, runtime, role="single")


def test_disposable_payload_copy_avoids_fsync_but_keeps_control_durable(sealed, monkeypatch):
    import stat
    source, _, _, _ = sealed
    original_fsync = native.os.fsync
    durable = []
    def fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            current = os.lseek(fd, 0, os.SEEK_CUR)
            os.lseek(fd, 0, os.SEEK_SET)
            # The durable control descriptor is write-only; locate its path
            # in our own fixture instead of reading or touching host files.
            controls = list(source.parent.rglob("control.json"))
            assert any((p.stat().st_dev, p.stat().st_ino) == (info.st_dev, info.st_ino) for p in controls), \
                "disposable payload must not synchronously fsync"
            os.lseek(fd, current, os.SEEK_SET)
            durable.append(info.st_ino)
        original_fsync(fd)
    monkeypatch.setattr(native.os, "fsync", fsync)
    bundle = prepare(sealed)
    assert durable == [(bundle.parent / "control.json").stat().st_ino]
    assert (bundle / "SRBMiner-MULTI").read_bytes() == (source / "SRBMiner-MULTI").read_bytes()


def test_explicit_closed_record_cleans_034_prefix_without_changing_legacy_slots(sealed):
    from agent.managed_miners import ManagedMinerManager
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    role_root = config.miner_packages_dir / native.LEGACY_NATIVE_SRB_RUNTIME_DIRECTORY / "single"
    role_root.mkdir(parents=True, mode=0o700)
    slot = role_root / "slot-0"
    slot.mkdir(mode=0o700)
    (slot / "keep").write_bytes(b"legacy not ours")
    bundle.parent.rename(role_root / bundle.parent.name)
    old_bundle = role_root / bundle.parent.name / "bundle"
    # This fixture run was never launched. Exercise the same exact-record
    # cleanup consumer used after a positive process stop/absence proof.
    manager = ManagedMinerManager(config)
    manager._discard_stopped_native_srb_record(
        {"miner_type": "srbminer", "executable": str(old_bundle / "SRBMiner-MULTI")}, runtime)
    assert not old_bundle.parent.exists()
    assert (slot / "keep").read_bytes() == b"legacy not ours"


def test_runtime_writes_leave_sealed_source_and_marker_unchanged(sealed):
    source, metadata, _, _ = sealed
    marker = (source / _MARKER_NAME).read_bytes()
    baseline = _tree_fingerprint(source, 1024**2, include_files=True)
    bundle = prepare(sealed)
    assert _tree_fingerprint(bundle, 1024**2, include_files=True) == baseline
    assert not (bundle / _MARKER_NAME).exists()
    assert (bundle / "empty").is_dir()
    for name in baseline["files"]:
        assert (source / name).stat().st_ino != (bundle / name).stat().st_ino
        assert (bundle / name).stat().st_nlink == 1
    (bundle / "Cache").mkdir()
    (bundle / "Cache/kernel.bin").write_bytes(b"generated kernel")
    (bundle / "autotune.json").write_bytes(b"generated tuning")
    (bundle / "SRBMiner-MULTI").write_bytes(b"runtime write")
    assert _tree_fingerprint(source, 1024**2, include_files=True) == baseline
    assert (source / _MARKER_NAME).read_bytes() == marker
    assert _verify_installed_package(source, metadata, 1024**2)["tree_sha256"] == baseline["tree_sha256"]


@pytest.mark.parametrize("change", ["existing_file", "new_file"])
def test_source_tampering_is_rejected_without_marker_repair(sealed, change):
    source, _, config, _ = sealed
    marker = (source / _MARKER_NAME).read_bytes()
    if change == "existing_file":
        (source / "SRBMiner-MULTI").write_bytes(b"modified executable")
    else:
        (source / "kernel.bin").write_bytes(b"generated kernel")
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)
    assert (source / _MARKER_NAME).read_bytes() == marker
    assert not (config.miner_packages_dir / native.NATIVE_SRB_RUNTIME_DIRECTORY).exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "marker_hardlink"])
def test_source_links_are_rejected(sealed, tmp_path, kind):
    source, _, _, _ = sealed
    if kind == "marker_hardlink":
        os.link(source / _MARKER_NAME, tmp_path / "marker-alias")
    else:
        target = source / "libraries/libexample.so"
        target.unlink()
        if kind == "symlink":
            target.symlink_to(source / "SRBMiner-MULTI")
        else:
            os.link(source / "SRBMiner-MULTI", target)
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)


@pytest.mark.parametrize("digest", [None, "b" * 64])
def test_unbound_or_mismatched_archive_sha_is_rejected(sealed, digest):
    sealed[1]["sha256"] = digest
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)


def test_fresh_generations_preserve_existing_live_and_prepared_bundles(sealed):
    first = prepare(sealed)
    (first / "runtime-extra").write_bytes(b"live runtime mutation")
    before = tree_bytes(first.parent)
    second = prepare(sealed)
    staged_before = tree_bytes(second.parent)
    third = prepare(sealed)
    assert len({first, second, third}) == 3
    assert tree_bytes(first.parent) == before
    assert tree_bytes(second.parent) == staged_before
    for bundle in (second, third):
        assert (bundle / "SRBMiner-MULTI").read_bytes() == b"sealed executable"
        assert not (bundle / "runtime-extra").exists()


def test_known_closed_discard_keeps_repeated_runs_from_accumulating(sealed):
    _, _, config, runtime = sealed
    older = prepare(sealed)
    (older / "keep").write_bytes(b"uncertain older run")
    older_before = tree_bytes(older.parent)
    role_root = older.parent.parent
    original_entries = set(role_root.iterdir())
    for _ in range(4):
        bundle = prepare(sealed)
        assert bundle != older
        (bundle / "Cache").mkdir()
        (bundle / "Cache/kernel.bin").write_bytes(b"generated cache")
        # No process is launched by this fixture: it is positively closed.
        assert native.discard_native_srb_bundle(bundle, config, runtime, role="single") is True
        assert not bundle.parent.exists()
        assert set(role_root.iterdir()) == original_entries
        assert tree_bytes(older.parent) == older_before






def test_generation_collision_rejects_without_reusing_existing_leaf(sealed, monkeypatch):
    first = prepare(sealed)
    (first / "keep").write_bytes(b"existing generation must survive")
    before = tree_bytes(first.parent)
    entries = set(first.parent.parent.iterdir())
    monkeypatch.setattr(native.uuid, "uuid4", lambda: SimpleNamespace(hex=first.parent.name.removeprefix("generation-")))
    with pytest.raises(native.NativeMinerRuntimeError, match="exclusively created"):
        prepare(sealed)
    assert tree_bytes(first.parent) == before
    assert set(first.parent.parent.iterdir()) == entries


@pytest.mark.parametrize("old_name", ["slot-0", "slot-1", "reclaiming", "generation-" + "b" * 32])
def test_unknown_old_object_does_not_block_fresh_generation_or_change_old_bytes(sealed, old_name):
    _, _, config, _ = sealed
    role_root = config.miner_packages_dir / native.NATIVE_SRB_RUNTIME_DIRECTORY / "single"
    old = role_root / old_name
    old.mkdir(parents=True, mode=0o700)
    (old / "control.json").write_bytes(b"{}")
    (old / "keep").write_bytes(b"unreviewed old object")
    before = tree_bytes(old)
    bundle = prepare(sealed)
    assert bundle.parent != old
    assert tree_bytes(old) == before
    assert (bundle / "SRBMiner-MULTI").read_bytes() == b"sealed executable"


def test_mutation_during_source_read_is_rejected(sealed, monkeypatch):
    source, _, _, _ = sealed
    marker = (source / _MARKER_NAME).read_bytes()
    original_copy = native._copy_file
    original_fdopen = native.os.fdopen
    changed = False
    class RacingStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size=-1):
            nonlocal changed
            data = self.stream.read(size)
            if data and not changed:
                changed = True
                (source / "SRBMiner-MULTI").write_bytes(b"changed in read")
            return data
    def racing_fdopen(descriptor, mode, **kwargs):
        stream = original_fdopen(descriptor, mode, **kwargs)
        return RacingStream(stream) if mode == "rb" else stream
    def racing_copy(*args):
        with monkeypatch.context() as context:
            context.setattr(native.os, "fdopen", racing_fdopen)
            return original_copy(*args)
    monkeypatch.setattr(native, "_copy_file", racing_copy)
    with pytest.raises(native.NativeMinerRuntimeError, match="changed while copying"):
        prepare(sealed)
    assert changed
    assert (source / _MARKER_NAME).read_bytes() == marker


def test_directory_swap_after_baseline_is_rejected(sealed, monkeypatch):
    source, _, _, _ = sealed
    original_copy = native._copy_file
    swapped = False
    def racing_copy(*args):
        nonlocal swapped
        if not swapped:
            swapped = True
            directory = source / "libraries"
            directory.rename(source / "libraries-original")
            directory.symlink_to(source / "libraries-original", target_is_directory=True)
        return original_copy(*args)
    monkeypatch.setattr(native, "_copy_file", racing_copy)
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)


def test_destination_parent_swap_is_rejected_before_file_creation(sealed, monkeypatch, tmp_path):
    original_copy = native._copy_file
    external = tmp_path / "external"
    external.mkdir()
    swapped = False
    def racing_copy(source, destination, *args):
        nonlocal swapped
        if not swapped:
            swapped = True
            destination.parent.rename(destination.parent.with_name("replaced"))
            destination.parent.symlink_to(external, target_is_directory=True)
        return original_copy(source, destination, *args)
    monkeypatch.setattr(native, "_copy_file", racing_copy)
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)
    assert list(external.iterdir()) == []


def test_role_bundles_and_localized_role_config_use_distinct_generations(sealed):
    primary = prepare(sealed, role="primary")
    secondary = prepare(sealed, role="secondary")
    assert primary != secondary
    assert primary.parent.parent.name == "primary"
    assert secondary.parent.parent.name == "secondary"
    _, metadata, config, runtime = sealed
    localized_config = replace(config, managed_miner_runtime_dir=runtime / "primary")
    next_primary = native.prepare_native_srb_bundle(sealed[0], metadata, localized_config, runtime / "primary",
                                                   role="primary")
    assert next_primary != primary and next_primary.parent.parent == primary.parent.parent
    assert (primary / "SRBMiner-MULTI").read_bytes() == b"sealed executable"
    with pytest.raises(native.NativeMinerRuntimeError, match="role"):
        native.prepare_native_srb_bundle(sealed[0], metadata, config, runtime, role="primary")


def test_base_named_primary_accepts_actual_primary_child(sealed):
    source, metadata, config, runtime = sealed
    base = runtime.with_name("primary")
    runtime.rename(base)
    child = base / "primary"
    child.mkdir(mode=0o700)
    base_config = replace(config, managed_miner_runtime_dir=base)
    bundle = native.prepare_native_srb_bundle(source, metadata, base_config, child, role="primary")
    assert bundle.parent.parent.name == "primary"
    control = json.loads((bundle.parent / "control.json").read_text(encoding="utf-8"))
    assert control["runtime"] == str(child)


def test_parent_components_cannot_escape_the_package_root(sealed):
    source, metadata, config, runtime = sealed
    external = source / ".." / ".." / "external"
    with pytest.raises(native.NativeMinerRuntimeError, match="parent component"):
        native.prepare_native_srb_bundle(external, metadata, config, runtime, role="single")


def test_copy_never_changes_permissions_through_a_path(sealed, monkeypatch):
    def unsafe_path_chmod(*args, **kwargs):
        raise AssertionError("runtime preparation must change permissions through the open handle")
    monkeypatch.setattr(Path, "chmod", unsafe_path_chmod)
    bundle = prepare(sealed)
    mode = (bundle / "SRBMiner-MULTI").stat().st_mode
    if os.name == "nt":
        assert mode & 0o200  # Windows stat exposes the writable flag, not POSIX x.
    else:
        assert mode & 0o100


def test_discard_preserves_directory_swapped_after_snapshot(sealed, monkeypatch):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    slot = bundle.parent
    saved = slot.parent / "saved-original"
    foreign = slot.parent / "foreign-directory"
    foreign.mkdir(mode=0o700)
    (foreign / "foreign-keep").write_bytes(b"unknown directory must survive")
    original_snapshot = native._snapshot
    swapped = False
    def racing_snapshot(path, **kwargs):
        nonlocal swapped
        result = original_snapshot(path, **kwargs)
        if Path(path) == slot and not swapped:
            swapped = True
            slot.rename(saved)
            foreign.rename(slot)
        return result
    monkeypatch.setattr(native, "_snapshot", racing_snapshot)
    with pytest.raises(native.NativeMinerRuntimeError, match="changed|identity"):
        native.discard_native_srb_bundle(bundle, config, runtime, role="single")
    assert swapped
    assert (slot / "foreign-keep").read_bytes() == b"unknown directory must survive"
    assert (saved / "bundle/SRBMiner-MULTI").read_bytes() == b"sealed executable"


def test_interrupted_reclamation_is_preserved_without_blocking_fresh_allocation(sealed):
    first = prepare(sealed)
    reclaim = first.parent.parent / "reclaiming"
    reclaim.mkdir(mode=0o700)
    (reclaim / "keep").write_bytes(b"unreviewed interrupted operation")
    before = tree_bytes(reclaim)
    second = prepare(sealed)
    assert second != first
    assert tree_bytes(reclaim) == before
    assert (first / "SRBMiner-MULTI").read_bytes() == b"sealed executable"


def test_lazy_directory_metadata_drift_preserves_file_attestation(sealed, monkeypatch):
    monkeypatch.setattr(native, "_windows_directory_metadata_is_lazy", lambda: True)
    original_lstat = Path.lstat
    calls = 0
    def lazy_directory_stat(path, *args, **kwargs):
        nonlocal calls
        info = original_lstat(path, *args, **kwargs)
        if not native.stat.S_ISDIR(info.st_mode):
            return info
        calls += 1
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        for name in ("st_size", "st_nlink", "st_mtime_ns", "st_ctime_ns"):
            values[name] += calls
        return SimpleNamespace(**values)
    monkeypatch.setattr(Path, "lstat", lazy_directory_stat)
    source, metadata, _, _ = sealed
    baseline = _tree_fingerprint(source, 1024**2, include_files=True)
    bundle = prepare(sealed)
    assert _tree_fingerprint(bundle, 1024**2, include_files=True) == baseline
    assert _verify_installed_package(source, metadata, 1024**2)["tree_sha256"] == baseline["tree_sha256"]
    (source / "SRBMiner-MULTI").write_bytes(b"genuine file tamper")
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)


def test_directory_inventory_is_rechecked_after_its_subtree(sealed, monkeypatch):
    monkeypatch.setattr(native, "_windows_directory_metadata_is_lazy", lambda: True)
    source = sealed[0]
    original_scandir = native.os.scandir
    added = False
    class RacingScan:
        def __init__(self, path):
            self.path, self.stream = Path(path), original_scandir(path)
        def __enter__(self):
            return self.stream.__enter__()
        def __exit__(self, *args):
            nonlocal added
            result = self.stream.__exit__(*args)
            if self.path == source / "libraries" and not added:
                added = True
                (source / "late-file.bin").write_bytes(b"added while inspecting a subtree")
            return result
    monkeypatch.setattr(native.os, "scandir", RacingScan)
    with pytest.raises(native.NativeMinerRuntimeError, match="entries changed"):
        native._snapshot(source, allow_marker=True)
    assert added


def test_non_windows_directory_metadata_keeps_full_identity(sealed, monkeypatch):
    monkeypatch.setattr(native, "_windows_directory_metadata_is_lazy", lambda: False)
    original = sealed[0].lstat()
    values = {name: getattr(original, name) for name in dir(original) if name.startswith("st_")}
    values["st_mtime_ns"] += 1
    changed = SimpleNamespace(**values)
    assert native._entry_identity(original) != native._entry_identity(changed)
    assert not native._unchanged({"": original}, {"": changed})


def test_windows_directory_attribute_hint_clearing_preserves_real_identity(sealed, monkeypatch):
    monkeypatch.setattr(native, "_windows_directory_metadata_is_lazy", lambda: True)
    original_lstat = Path.lstat
    original_fstat = os.fstat
    seen = set()
    def directory_attribute_transition(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if not native.stat.S_ISDIR(info.st_mode):
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values["st_file_attributes"] = 0x10 if str(path) in seen else 0x10000010
        values["st_reparse_tag"] = 0
        seen.add(str(path))
        return SimpleNamespace(**values)
    monkeypatch.setattr(Path, "lstat", directory_attribute_transition)
    def stable_directory_handle(fd):
        info = original_fstat(fd)
        if not native.stat.S_ISDIR(info.st_mode):
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        values.update(st_file_attributes=0x10, st_reparse_tag=0)
        return SimpleNamespace(**values)
    monkeypatch.setattr(os, "fstat", stable_directory_handle)
    source, metadata, _, _ = sealed
    baseline = _tree_fingerprint(source, 1024**2, include_files=True)
    bundle = prepare(sealed)
    assert _tree_fingerprint(bundle, 1024**2, include_files=True) == baseline
    assert _verify_installed_package(source, metadata, 1024**2)["tree_sha256"] == baseline["tree_sha256"]
    info = directory_attribute_transition(bundle)
    for field, value in (("st_ino", info.st_ino + 1), ("st_mode", info.st_mode ^ 0o100),
                         ("st_file_attributes", 0x12), ("st_file_attributes", 0x410),
                         ("st_reparse_tag", 0xA0000003)):
        changed = SimpleNamespace(**vars(info))
        setattr(changed, field, value)
        assert native._directory_identity(info) != native._directory_identity(changed)
    info.st_file_attributes = 0x410
    monkeypatch.setattr(Path, "lstat", lambda path, *args, **kwargs: info)
    with pytest.raises(native.NativeMinerRuntimeError, match="plain directories"):
        native._directory(bundle)


@pytest.mark.parametrize("owner_kind", ["legacy", "other-session"])
def test_discard_retains_legacy_and_another_session_generations(sealed, owner_kind):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    control = bundle.parent / "control.json"
    record = json.loads(control.read_text())
    if owner_kind == "legacy":
        record["kind"] = "rigpilot-native-srb-runtime.v1"
        for key in ("generation", "owner", "directory_identity"):
            record.pop(key)
        control.write_text(json.dumps(record))
        old = bundle.parent.with_name("slot-0")
        bundle.parent.rename(old)
        bundle = old / "bundle"
    else:
        record["owner"] = "f" * 32 if native._OWNER != "f" * 32 else "e" * 32
        control.write_text(json.dumps(record))
    before = tree_bytes(bundle.parent)
    assert native.discard_native_srb_bundle(bundle, config, runtime, role="single") is False
    assert tree_bytes(bundle.parent) == before


def test_discard_rejects_copied_control_on_a_different_root_identity(sealed, tmp_path):
    import shutil
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    saved = tmp_path / "original-generation"
    bundle.parent.rename(saved)
    shutil.copytree(saved, bundle.parent)
    before = tree_bytes(bundle.parent)
    with pytest.raises(native.NativeMinerRuntimeError, match="identity"):
        native.discard_native_srb_bundle(bundle, config, runtime, role="single")
    assert tree_bytes(bundle.parent) == before
    assert tree_bytes(saved) == before


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_discard_retains_linked_generation_and_external_target(sealed, tmp_path, link_kind):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    external = tmp_path / "external-keep"
    external.write_bytes(b"external bytes must survive")
    link = bundle / "generated-link"
    if link_kind == "symlink":
        link.symlink_to(external)
    else:
        os.link(external, link)
    before = tree_bytes(bundle.parent)
    with pytest.raises(native.NativeMinerRuntimeError, match="link|linked"):
        native.discard_native_srb_bundle(bundle, config, runtime, role="single")
    assert tree_bytes(bundle.parent) == before
    assert external.read_bytes() == b"external bytes must survive"


def test_copy_failure_discards_only_its_unexposed_generation(sealed, monkeypatch):
    first = prepare(sealed)
    (first / "keep").write_bytes(b"existing prepared generation")
    before = tree_bytes(first.parent)
    entries = set(first.parent.parent.iterdir())
    original_copy = native._copy_file
    def interrupted_copy(*args):
        original_copy(*args)
        raise OSError("injected copy failure")
    monkeypatch.setattr(native, "_copy_file", interrupted_copy)
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)
    assert set(first.parent.parent.iterdir()) == entries
    assert tree_bytes(first.parent) == before


def test_insufficient_actual_disk_space_does_not_create_a_generation(sealed, monkeypatch):
    source, _, config, _ = sealed
    required = _tree_fingerprint(source, 1024**2)["extracted_bytes"] + native._MAX_CONTROL_BYTES
    monkeypatch.setattr(native.shutil, "disk_usage", lambda path: SimpleNamespace(free=required - 1))
    with pytest.raises(native.NativeMinerRuntimeError, match="insufficient disk space"):
        prepare(sealed)
    role_root = config.miner_packages_dir / native.NATIVE_SRB_RUNTIME_DIRECTORY / "single"
    assert not role_root.exists() or list(role_root.iterdir()) == []
    monkeypatch.setattr(native.shutil, "disk_usage", lambda path: SimpleNamespace(free=required))
    bundle = prepare(sealed)
    assert (bundle / "SRBMiner-MULTI").read_bytes() == b"sealed executable"


def test_discard_rename_race_never_deletes_foreign_directory(sealed, monkeypatch):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    slot = bundle.parent
    saved = slot.parent / "saved-owned-generation"
    foreign = slot.parent / "foreign-directory"
    foreign.mkdir(mode=0o700)
    (foreign / "foreign-keep").write_bytes(b"unknown directory must survive")
    owned_identity = (slot.stat().st_dev, slot.stat().st_ino)
    foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
    assert owned_identity != foreign_identity
    original_rename = native.os.rename
    swapped = False
    def racing_rename(source, destination, **kwargs):
        nonlocal swapped
        if Path(source) == slot and not swapped:
            swapped = True
            original_rename(slot, saved)
            original_rename(foreign, slot)
        return original_rename(source, destination, **kwargs)
    monkeypatch.setattr(native.os, "rename", racing_rename)
    with pytest.raises(native.NativeMinerRuntimeError, match="changed|identity"):
        native.discard_native_srb_bundle(bundle, config, runtime, role="single")
    assert swapped
    assert (saved.stat().st_dev, saved.stat().st_ino) == owned_identity
    assert (saved / "bundle/SRBMiner-MULTI").read_bytes() == b"sealed executable"
    preserved = list(slot.parent.rglob("foreign-keep"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == b"unknown directory must survive"
    assert (preserved[0].parent.stat().st_dev, preserved[0].parent.stat().st_ino) == foreign_identity



def test_confirmed_closed_previous_session_generation_can_be_discarded(sealed):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    control = bundle.parent / "control.json"
    record = json.loads(control.read_text())
    record["owner"] = "f" * 32 if native._OWNER != "f" * 32 else "e" * 32
    control.write_text(json.dumps(record))
    # This prepared fixture was never handed to a process. The caller has
    # positive closed proof; ordinary lease release would not authorize this.
    assert native.discard_native_srb_bundle(bundle, config, runtime, role="single", confirmed_stop=True) is True
    assert not bundle.parent.exists()


def test_discard_invalid_ownership_control_preserves_generation(sealed):
    _, _, config, runtime = sealed
    bundle = prepare(sealed)
    control = bundle.parent / "control.json"
    record = json.loads(control.read_text())
    record.pop("owner")
    control.write_text(json.dumps(record))
    before = tree_bytes(bundle.parent)
    with pytest.raises(native.NativeMinerRuntimeError, match="ownership record"):
        native.discard_native_srb_bundle(bundle, config, runtime, role="single", confirmed_stop=True)
    assert tree_bytes(bundle.parent) == before


def test_failed_copy_preserves_substituted_generation_and_existing_run(sealed, monkeypatch):
    _, _, config, runtime = sealed
    older = prepare(sealed)
    older_before = tree_bytes(older.parent)
    role_root = older.parent.parent
    foreign = role_root / "foreign-copy-cleanup"
    foreign.mkdir(mode=0o700)
    (foreign / "foreign-keep").write_bytes(b"foreign copy cleanup bytes")
    saved = role_root / "saved-partial-generation"
    original_copy = native._copy_file
    substituted = []
    def interrupted_copy(source, destination, *args):
        original_copy(source, destination, *args)
        bundle = destination.parent
        while bundle.name != "bundle":
            bundle = bundle.parent
        generation = bundle.parent
        generation.rename(saved)
        foreign.rename(generation)
        substituted.append(generation)
        raise OSError("injected failure after generation substitution")
    monkeypatch.setattr(native, "_copy_file", interrupted_copy)
    with pytest.raises(native.NativeMinerRuntimeError):
        prepare(sealed)
    assert len(substituted) == 1
    assert (substituted[0] / "foreign-keep").read_bytes() == b"foreign copy cleanup bytes"
    assert (saved / "bundle/SRBMiner-MULTI").read_bytes() == b"sealed executable"
    assert tree_bytes(older.parent) == older_before
