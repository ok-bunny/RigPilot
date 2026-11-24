"""Build selection and offline dependency contracts, using fake inputs only."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "deploy" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_elf_header(machine=62, elf_class=2, byte_order=1):
    """Non-executable bytes for static-header tests; never a native smoke proof."""
    value = bytearray(64)
    value[:7] = b"\x7fELF" + bytes([elf_class, byte_order, 1])
    value[18:20] = machine.to_bytes(2, "little")
    return bytes(value)


def fake_pe_header(machine=0x8664, optional_magic=0x20B):
    """A test header only: it cannot run or establish real hardware compatibility."""
    value = bytearray(128)
    value[:2] = b"MZ"
    value[60:64] = (64).to_bytes(4, "little")
    value[64:68] = b"PE\x00\x00"
    value[68:70] = machine.to_bytes(2, "little")
    value[88:90] = optional_magic.to_bytes(2, "little")
    return bytes(value)


@pytest.mark.parametrize("target", ["controller", "linux"])
def test_aggregate_does_not_import_a_skipped_component(tmp_path, target):
    project = tmp_path / "source with spaces"
    deploy = project / "deploy"
    (deploy / "linux").mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/build-all-releases.sh", deploy)
    selected = project / ("server" if target == "controller" else "agent")
    selected.mkdir()
    (selected / "__init__.py").write_text('__version__ = "1.2.3"\n')
    if target == "linux":
        (selected / "version.py").write_text('__version__ = "1.2.3"\n')
    # The unselected component is absent, rather than merely having a valid version.
    recorder = (
        "import json,sys\nfrom pathlib import Path\n"
        "p=Path(__file__).resolve()\n"
        "with (p.parent.parent/'calls.jsonl').open('a') as f:\n"
        " f.write(json.dumps({'name':p.stem,'args':sys.argv[1:]})+'\\n')\n"
    )
    for name in ("build-controller-release.py", "verify-release-artifacts.py"):
        (deploy / name).write_text(recorder)
    (deploy / "linux/build-ubuntu-portable.sh").write_text("#!/bin/bash\nexit 0\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text("#!/bin/bash\nprintf 'Linux\\n'\n")
    (fake_bin / "uname").chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)
    output = tmp_path / "output"
    skip = "--skip-linux" if target == "controller" else "--skip-controller"
    result = subprocess.run(
        ["bash", str(deploy / "build-all-releases.sh"), skip, "--output-dir", str(output)],
        cwd=tmp_path, env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (project / "calls.jsonl").read_text().splitlines()]
    verifier = calls[-1]["args"]
    chosen = "--controller-version" if target == "controller" else "--agent-version"
    omitted = "--agent-version" if target == "controller" else "--controller-version"
    assert chosen in verifier and omitted not in verifier


@pytest.mark.parametrize("target,skips,version_option", [
    ("controller", ["--skip-windows", "--skip-linux"], "--controller-version"),
    ("windows", ["--skip-linux", "--skip-controller"], "--agent-version"),
])
def test_verifier_only_requires_the_selected_component(tmp_path, monkeypatch, target, skips, version_option):
    verifier = load("verify-release-artifacts.py")
    calls = []
    monkeypatch.setattr(verifier, "verify_" + target, lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["verify", "--output-dir", str(tmp_path), *skips, version_option, "1.2.3"])
    assert verifier.main() == 0
    assert len(calls) == 1 and calls[0][1] == "1.2.3"


def test_verifier_rejects_an_empty_selection_before_reading_output(tmp_path, monkeypatch, capsys):
    verifier = load("verify-release-artifacts.py")
    monkeypatch.setattr(sys, "argv", ["verify", "--output-dir", str(tmp_path / "absent"),
        "--skip-windows", "--skip-linux", "--skip-controller", "--agent-version", "1.2.3", "--controller-version", "1.2.3"])
    with pytest.raises(SystemExit) as error:
        verifier.main()
    assert error.value.code == 2
    assert "No release target selected" in capsys.readouterr().err


@pytest.mark.parametrize("value", [None, "invalid"])
def test_selected_version_still_required(tmp_path, monkeypatch, value):
    verifier = load("verify-release-artifacts.py")
    argv = ["verify", "--output-dir", str(tmp_path), "--skip-windows", "--skip-linux"]
    if value is not None:
        argv += ["--controller-version", value]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(verifier, "verify_controller", lambda *args: pytest.fail("must reject before verifier"))
    with pytest.raises(SystemExit) as error:
        verifier.main()
    assert error.value.code == 2


def fake_runtime(tmp_path, builder, count):
    wheelhouse = tmp_path / builder.WHEELHOUSE
    wheelhouse.mkdir(parents=True)
    wheels = []
    lock = []
    for index in range(count):
        name = f"fixture_{index}"
        wheel = wheelhouse / f"{name}-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"{name}-1.0.dist-info/METADATA", f"Name: {name}\nVersion: 1.0\n")
        wheels.append(wheel.relative_to(tmp_path).as_posix())
        lock.append(f"{name}==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n")
    runtime = tmp_path / builder.RUNTIME_ROOT
    (runtime / "requirements-controller-linux-py310.lock").write_text("".join(lock))
    (tmp_path / "server").mkdir()
    (tmp_path / "server/requirements.txt").write_text("fixture_0==1.0\n")
    target = {
        "schema_version": 1, "controller_version": "1.2.3",
        "platform": {"system": "Linux", "machine": "x86_64", "glibc_minimum": "2.35"},
        "python": {"implementation": "CPython", "major_minor": "3.10", "validated_patch": "3.10.12", "abi": "cp310"},
        "dependencies": {"lock": builder.RUNTIME_ROOT + "/requirements-controller-linux-py310.lock",
            "wheelhouse": builder.WHEELHOUSE, "wheel_count": count, "only_binary": True, "require_hashes": True},
    }
    (runtime / "runtime-target.json").write_text(json.dumps(target))
    builder.PROJECT_ROOT = tmp_path
    builder.ALLOWED_FILES = tuple(wheels)
    return wheelhouse


@pytest.fixture
def controller_source(tmp_path):
    """Use only synthetic source and wheels, never build a formal source package."""
    builder = load("build-controller-release.py")
    project = tmp_path / "source with spaces"
    fake_runtime(project, builder, 2)
    (project / "server/__init__.py").write_text('__version__ = "1.2.3"\n')
    builder.ALLOWED_FILES += (
        "server/__init__.py", "server/requirements.txt",
        builder.RUNTIME_ROOT + "/runtime-target.json",
        builder.RUNTIME_ROOT + "/requirements-controller-linux-py310.lock",
    )
    return builder


def test_controller_release_builder_is_allowlisted_atomic_and_deterministic(tmp_path, controller_source):
    builder = controller_source
    first, sidecar, digest = builder.build("1.2.3", tmp_path / "first")
    second, _, second_digest = builder.build("1.2.3", tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    assert digest == second_digest == builder.sha256(first)
    assert sidecar.read_text() == f"{digest}  {first.name}\n"
    with tarfile.open(first, "r:gz") as archive:
        members = archive.getmembers()
        manifest = json.load(archive.extractfile("controller-release.json"))
        files = {member.name for member in members if member.isfile()}
    assert manifest == {"schema_version": 1, "version": "1.2.3",
                        "files": builder.validate_sources("1.2.3")}
    assert files == set(builder.ALLOWED_FILES) | {"controller-release.json"}
    for member in members:
        assert member.uid == member.gid == member.mtime == 0
        assert member.uname == member.gname == ""
        assert member.mode == (0o755 if member.isdir() else 0o644)
    load("verify-release-artifacts.py").verify_controller(first, "1.2.3")
    assert not list(first.parent.glob("*.part"))


def test_controller_builder_refuses_source_changes_during_packaging(tmp_path, monkeypatch, controller_source):
    builder = controller_source
    validate = builder.validate_sources

    def change_after_validation(version):
        hashes = validate(version)
        requirements = builder.PROJECT_ROOT / "server/requirements.txt"
        requirements.write_text(requirements.read_text() + "# concurrent edit\n")
        return hashes

    monkeypatch.setattr(builder, "validate_sources", change_after_validation)
    output = tmp_path / "output"
    with pytest.raises(RuntimeError, match="source changed while building"):
        builder.build("1.2.3", output)
    assert list(output.iterdir()) == []


@pytest.mark.parametrize("existing", ["archive", "sidecar", "archive-link", "sidecar-link"])
def test_controller_builder_refuses_existing_outputs(tmp_path, controller_source, existing):
    output = tmp_path / "output"
    output.mkdir()
    name = "RigPilotController-1.2.3.tar.gz" + (".sha256" if existing.startswith("sidecar") else "")
    path = output / name
    if existing.endswith("link"):
        path.symlink_to(output / "missing")
    else:
        path.write_bytes(b"keep existing output")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        controller_source.build("1.2.3", output)
    assert list(output.iterdir()) == [path]
    if existing.endswith("link"):
        assert path.is_symlink()
    else:
        assert path.read_bytes() == b"keep existing output"


@pytest.mark.parametrize("phase", ["archive-write", "archive-publish", "sidecar-write", "sidecar-publish"])
def test_controller_builder_recovers_failed_publication(tmp_path, monkeypatch, controller_source, phase):
    builder = controller_source
    output = tmp_path / "output"

    def fail(*args):
        raise OSError(phase)

    if phase == "archive-write":
        monkeypatch.setattr(builder.tarfile.TarFile, "addfile", fail)
    elif phase == "sidecar-write":
        monkeypatch.setattr(builder.os, "fsync", fail)
    else:
        original = builder.os.link

        def fail_publish(path, target):
            is_sidecar = Path(target).suffix == ".sha256"
            if is_sidecar == (phase == "sidecar-publish"):
                raise OSError(phase)
            return original(path, target)

        monkeypatch.setattr(builder.os, "link", fail_publish)
    with pytest.raises(OSError, match=phase):
        builder.build("1.2.3", output)
    expected = [output / "RigPilotController-1.2.3.tar.gz"] if phase == "sidecar-publish" else []
    assert list(output.iterdir()) == expected


@pytest.mark.parametrize("phase", ["archive", "sidecar"])
@pytest.mark.parametrize("linked", [False, True])
def test_controller_builder_does_not_clobber_concurrent_outputs(tmp_path, monkeypatch, controller_source, capsys, phase, linked):
    output = tmp_path / "output"
    name = "RigPilotController-1.2.3.tar.gz" + (".sha256" if phase == "sidecar" else "")
    competitor = output / name
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep concurrent output")
    original = tempfile.NamedTemporaryFile

    def create_competitor(*args, **kwargs):
        # Introduce the collision after the early exists check, during staging.
        staged = original(*args, **kwargs)
        if kwargs["prefix"] == f".{name}.":
            if linked:
                competitor.symlink_to(unrelated)
            else:
                competitor.write_bytes(unrelated.read_bytes())
        return staged

    monkeypatch.setattr(controller_source.tempfile, "NamedTemporaryFile", create_competitor)
    with pytest.raises(FileExistsError):
        controller_source.build("1.2.3", output)
    expected = {competitor}
    if phase == "sidecar":
        expected.add(output / "RigPilotController-1.2.3.tar.gz")
    assert set(output.iterdir()) == expected
    assert ("partial publication" in capsys.readouterr().err) is (phase == "sidecar")
    assert competitor.is_symlink() is linked
    assert competitor.read_bytes() == unrelated.read_bytes() == b"keep concurrent output"


@pytest.mark.parametrize("linked", [False, True])
def test_controller_builder_does_not_withdraw_replaced_archive(tmp_path, monkeypatch, controller_source, capsys, linked):
    output = tmp_path / "output"
    target = output / "RigPilotController-1.2.3.tar.gz"
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"another writer")
    original = controller_source.os.link

    def replace_archive_before_sidecar(source, destination):
        if Path(destination).suffix == ".sha256":
            target.unlink()
            if linked:
                target.symlink_to(unrelated)
            else:
                target.write_bytes(unrelated.read_bytes())
            raise OSError("sidecar publication failed")
        return original(source, destination)

    monkeypatch.setattr(controller_source.os, "link", replace_archive_before_sidecar)
    with pytest.raises(OSError, match="sidecar publication failed"):
        controller_source.build("1.2.3", output)
    assert target.exists() and target.is_symlink() is linked
    assert target.read_bytes() == unrelated.read_bytes() == b"another writer"
    assert "partial publication" in capsys.readouterr().err
    assert list(output.iterdir()) == [target]


@pytest.mark.parametrize("phase", ["packaging-failure", "publication-failure", "success", "success-in-outer-handler"])
def test_controller_builder_cleanup_preserves_primary_failure(tmp_path, monkeypatch, controller_source, capsys, phase):
    builder = controller_source
    output = tmp_path / "output"
    original_unlink, original_link = Path.unlink, builder.os.link

    def fail_cleanup(path, *args, **kwargs):
        if path.name.endswith(".part"):
            raise OSError("private cleanup failure")
        return original_unlink(path, *args, **kwargs)

    def fail_publication(source, destination):
        if Path(destination).suffix == ".sha256":
            raise OSError("primary failure")
        return original_link(source, destination)

    if phase == "packaging-failure":
        def fail_packaging(*_args): raise OSError("primary failure")
        monkeypatch.setattr(builder.tarfile.TarFile, "addfile", fail_packaging)
    elif phase == "publication-failure":
        monkeypatch.setattr(builder.os, "link", fail_publication)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    if phase.startswith("success"):
        if phase == "success-in-outer-handler":
            try:
                raise ValueError("caller's previously handled error")
            except ValueError:
                builder.build("1.2.3", output)
        else:
            builder.build("1.2.3", output)
    else:
        with pytest.raises(OSError, match="primary failure"):
            builder.build("1.2.3", output)
    assert "private cleanup failure" in capsys.readouterr().err
    target = output / "RigPilotController-1.2.3.tar.gz"
    assert target.exists() is (phase != "packaging-failure")
    if phase.startswith("success"):
        load("verify-release-artifacts.py").verify_controller(target, "1.2.3")


@pytest.mark.parametrize("name", ["server/.DS_Store", "server/rogue", "server/linked-directory"])
def test_controller_builder_rejects_unlisted_links(tmp_path, controller_source, name):
    # Include dangling and directory links in source closure instead of treating
    # them as absent regular files or harmless Finder metadata.
    project = controller_source.PROJECT_ROOT
    target = project / "server" if name.endswith("directory") else project / "absent"
    (project / name).symlink_to(target)
    with pytest.raises(RuntimeError, match="allowlist drift"):
        controller_source.validate_sources("1.2.3")


@pytest.mark.parametrize("requirements", ["", "fixture_0>=1.0\n", "fixture_0==1.0\n--extra-index-url=https://example.invalid\n"])
def test_runtime_closure_rejects_nonexact_development_pins(controller_source, requirements):
    (controller_source.PROJECT_ROOT / "server/requirements.txt").write_text(requirements)
    with pytest.raises(RuntimeError, match="only exact pins"):
        controller_source.validate_runtime_closure("1.2.3")


@pytest.mark.parametrize("version", ["0.0.0", "2.10.30", "01.2.3", "1.02.3", "1.2.03"])
def test_controller_source_and_package_use_the_same_canonical_version(tmp_path, controller_source, version):
    builder = controller_source
    (builder.PROJECT_ROOT / "server/__init__.py").write_text(f'__version__ = "{version}"\n')
    target_path = builder.PROJECT_ROOT / builder.RUNTIME_ROOT / "runtime-target.json"
    target = json.loads(target_path.read_text())
    target["controller_version"] = version
    target_path.write_text(json.dumps(target))
    if version in {"0.0.0", "2.10.30"}:
        assert builder.source_version() == version
        path, _, _ = builder.build(version, tmp_path / "output")
        load("verify-release-artifacts.py").verify_controller(path, version)
    else:
        with pytest.raises(RuntimeError, match="cannot read controller source version"):
            builder.source_version()
        with pytest.raises(RuntimeError, match="version must be X.Y.Z"):
            builder.build(version, tmp_path / "output")
        assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("component", ["builder", "controller", "agent"])
def test_release_cli_rejects_noncanonical_version_before_touching_output(tmp_path, monkeypatch, component):
    module = load("build-controller-release.py" if component == "builder" else "verify-release-artifacts.py")
    argv = ["release", "--output-dir", str(tmp_path / "absent")]
    if component == "builder":
        argv += ["--version", "01.2.3"]
    elif component == "controller":
        argv += ["--skip-windows", "--skip-linux", "--controller-version", "01.2.3"]
    else:
        argv += ["--skip-linux", "--skip-controller", "--agent-version", "01.2.3"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    assert not (tmp_path / "absent").exists()


def write_tar_fixture(tmp_path, members):
    path = tmp_path / "fixture.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, payload, mode, kind in members:
            info = tarfile.TarInfo(name)
            info.mode, info.type = mode, kind
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    path.with_name(path.name + ".sha256").write_text(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
    )
    return path


@pytest.mark.parametrize("platform", ["linux", "windows", "controller"])
@pytest.mark.parametrize("fault", [
    "not-object", "schema", "schema-bool", "schema-float", "schema-none",
    "version", "empty", "path", "digest", "case-collision", "parent-file",
])
def test_all_manifests_reject_invalid_identity_and_file_maps(tmp_path, platform, fault):
    verifier = load("verify-release-artifacts.py")
    manifest = {"schema_version": 1, "version": "1.2.3", "files": {"program": "a" * 64}}
    if platform != "controller":
        manifest.update(platform=platform, architecture="x86_64")
    if fault == "not-object":
        manifest = None
    elif fault.startswith("schema"):
        manifest["schema_version"] = {
            "schema": "wrong", "schema-bool": True, "schema-float": 1.0, "schema-none": None,
        }[fault]
    elif fault == "version":
        manifest["version"] = "wrong"
    elif fault == "empty":
        manifest["files"] = {}
    elif fault == "path":
        manifest["files"] = {"../program": "a" * 64}
    elif fault == "digest":
        manifest["files"] = {"program": "not a digest"}
    elif fault == "parent-file":
        manifest["files"] = {"Program": "a" * 64, "program/child": "a" * 64}
    else:
        manifest["files"] = {"Program": "a" * 64, "program": "a" * 64}
    with pytest.raises(RuntimeError, match=("parent directory" if fault == "parent-file"
                                          else "identity" if fault.startswith("schema") else None)):
        if platform == "controller":
            path = write_tar_fixture(tmp_path, [("controller-release.json", json.dumps(manifest).encode(), 0o644, tarfile.REGTYPE)])
            verifier.verify_controller(path, "1.2.3")
        else:
            verifier.verify_manifest(manifest, version="1.2.3", platform=platform, architecture="x86_64")


@pytest.mark.parametrize("name", ["", ".", "a\x00b", "/absolute", "../parent", "a/../parent", "a//b", "./a", "a/./b", "a\\b"])
def test_member_paths_reject_absolute_traversal_and_noncanonical_names(name):
    with pytest.raises(RuntimeError, match="unsafe release member"):
        load("verify-release-artifacts.py").canonical(name)


@pytest.mark.parametrize("fault", [None, "file-mode", "directory-mode", "hash", "link", "crlf"])
def test_linux_fixture_checks_payload_permissions_and_member_type(tmp_path, fault):
    verifier = load("verify-release-artifacts.py")
    root = "RigPilotAgent-Ubuntu-x64-OnlineUpdate"
    version = "0.0.0" if fault == "crlf" else "1.2.3"
    payload = fake_elf_header()
    manifest = {"schema_version": 1, "version": version, "platform": "linux", "architecture": "x86_64",
                "files": {"RigPilotAgent": hashlib.sha256(payload).hexdigest()}}
    path = write_tar_fixture(tmp_path, [
        (root, b"", 0o700 if fault == "directory-mode" else 0o755, tarfile.DIRTYPE),
        (root + "/RigPilotAgent", payload + (b"changed" if fault == "hash" else b""),
         0o644 if fault == "file-mode" else 0o755, tarfile.SYMTYPE if fault == "link" else tarfile.REGTYPE),
        (root + "/agent-release.json", (json.dumps(manifest) + ("\r\n" if fault == "crlf" else "\n")).encode(),
         0o644, tarfile.REGTYPE),
    ])
    if fault is None:
        verifier.verify_linux(path, version, online=True)
    else:
        with pytest.raises(RuntimeError, match="must use LF only" if fault == "crlf" else None):
            verifier.verify_linux(path, version, online=True)


@pytest.mark.parametrize("fault", ["negative-size", "sparse", "directory-data", "entry-count"])
def test_tar_reader_rejects_invalid_metadata_before_payload_reads(tmp_path, monkeypatch, fault):
    verifier = load("verify-release-artifacts.py")
    visited = []

    class Archive:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def __iter__(self):
            for index in range(4097 if fault == "entry-count" else 1):
                visited.append(index)
                member = tarfile.TarInfo(f"directory-{index}")
                member.type = tarfile.DIRTYPE
                if fault == "negative-size": member.size = -1
                elif fault == "sparse": member.sparse = [(0, 1)]
                elif fault == "directory-data": member.size = 1
                yield member
        def getmembers(self): return list(self)
        def extractfile(self, _): pytest.fail("invalid metadata must fail before reading payload")

    monkeypatch.setattr(verifier.tarfile, "open", lambda *args: Archive())
    with pytest.raises(RuntimeError):
        verifier.regular_tar_members(tmp_path / "unused.tar.gz")
    assert len(visited) == (4097 if fault == "entry-count" else 1)


def test_controller_rejects_directories_outside_manifest_parents(tmp_path):
    path = write_tar_fixture(tmp_path, [
        ("server/program", b"payload", 0o644, tarfile.REGTYPE),
        ("rogue-directory", b"", 0o755, tarfile.DIRTYPE),
        ("controller-release.json", json.dumps({"schema_version": 1, "version": "1.2.3",
         "files": {"server/program": hashlib.sha256(b"payload").hexdigest()}}).encode(), 0o644, tarfile.REGTYPE),
    ])
    with pytest.raises(RuntimeError, match="unexpected directory"):
        load("verify-release-artifacts.py").verify_controller(path, "1.2.3")


@pytest.mark.parametrize("fault", [None, "link", "special", "oversized"])
def test_windows_fixture_rejects_links_special_and_oversized_members(tmp_path, monkeypatch, fault):
    verifier = load("verify-release-artifacts.py")
    path = windows_package(tmp_path, verifier)
    if fault:
        original = zipfile.ZipFile.infolist

        def unsafe_members(archive):
            members = original(archive)
            member = members[0]
            if fault == "oversized":
                member.file_size = 513 * 1024 * 1024
            else:
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK if fault == "link" else stat.S_IFIFO) << 16
            return members

        monkeypatch.setattr(zipfile.ZipFile, "infolist", unsafe_members)
        with pytest.raises(RuntimeError):
            verifier.verify_windows(path, "1.2.3")
    else:
        verifier.verify_windows(path, "1.2.3")


@pytest.mark.parametrize("count", [2, 3])
def test_wheel_count_is_derived_from_the_declared_allowlist(tmp_path, count):
    builder = load("build-controller-release.py")
    fake_runtime(tmp_path, builder, count)
    builder.validate_runtime_closure("1.2.3")


@pytest.mark.parametrize("consumer", ["builder", "installer"])
@pytest.mark.parametrize("field,value,count", [
    ("schema_version", True, 2), ("schema_version", 1.0, 2),
    ("wheel_count", 2.0, 2), ("wheel_count", True, 1),
    ("only_binary", 1, 2), ("only_binary", 1.0, 2),
    ("require_hashes", 1, 2), ("require_hashes", 1.0, 2),
])
def test_runtime_target_requires_json_integer_and_boolean_types(tmp_path, consumer, field, value, count):
    builder = load("build-controller-release.py")
    fake_runtime(tmp_path, builder, count)
    target_path = tmp_path / builder.RUNTIME_ROOT / "runtime-target.json"
    target = json.loads(target_path.read_text())
    fields = target if field == "schema_version" else target["dependencies"]
    fields[field] = value
    target_path.write_text(json.dumps(target))
    with pytest.raises(RuntimeError, match="runtime target"):
        if consumer == "builder":
            builder.validate_runtime_closure("1.2.3")
        else:
            run_installer_runtime_closure(tmp_path, builder)


@pytest.mark.parametrize("drift", ["unexpected", "hash", "count", "identity"])
def test_dynamic_wheel_count_preserves_exact_closure(tmp_path, drift):
    builder = load("build-controller-release.py")
    fake_runtime(tmp_path, builder, 2)
    drift_runtime(tmp_path, builder, drift)
    with pytest.raises(RuntimeError):
        builder.validate_runtime_closure("1.2.3")


def drift_runtime(tmp_path, builder, drift):
    """Mutate fake inputs; builder and embedded installer remain separate consumers."""
    wheelhouse = tmp_path / builder.WHEELHOUSE
    wheel = next(wheelhouse.glob("*.whl"))
    if drift == "unexpected":
        (wheelhouse / "unexpected.whl").write_bytes(b"unapproved")
    elif drift == "missing":
        wheel.unlink()
    elif drift == "hash":
        wheel.write_bytes(wheel.read_bytes() + b"damaged")
    elif drift == "identity":
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("other-2.0.dist-info/METADATA", "Name: other\nVersion: 2.0\n")
        lock = tmp_path / builder.RUNTIME_ROOT / "requirements-controller-linux-py310.lock"
        lines = lock.read_text().splitlines()
        lines = [line.split("--hash=sha256:")[0] + "--hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
                 if line.startswith(wheel.name.split("-", 1)[0] + "==") else line for line in lines]
        lock.write_text("\n".join(lines) + "\n")
    else:
        target_path = tmp_path / builder.RUNTIME_ROOT / "runtime-target.json"
        target = json.loads(target_path.read_text())
        target["dependencies"]["wheel_count"] = 99
        target_path.write_text(json.dumps(target))


def run_installer_runtime_closure(tmp_path, builder, overrides=None):
    """Execute only the real embedded Python closure validator on fake wheels."""
    source = (ROOT / "deploy/deploy-controller-release.sh").read_text()
    begin = source.index("expected_wheels = {name for name in allowed_files")
    end = source.index("\nPY\n", begin)
    from email.parser import BytesParser
    from email.policy import compat32
    import re

    class LibcVersion:
        restype = None

        def __call__(self):
            return b"2.35"

    libc = LibcVersion()
    namespace = {
        "stage": tmp_path, "allowed_files": set(builder.ALLOWED_FILES),
        "runtime_root": builder.RUNTIME_ROOT, "wheelhouse": builder.WHEELHOUSE,
        "expected_version": "1.2.3", "Path": Path, "json": json,
        "hashlib": hashlib, "zipfile": zipfile, "re": re,
        "BytesParser": BytesParser, "compat32": compat32,
        "platform": SimpleNamespace(system=lambda: "Linux", machine=lambda: "x86_64",
                                    python_implementation=lambda: "CPython"),
        "sys": SimpleNamespace(version_info=(3, 10)),
        "ctypes": SimpleNamespace(CDLL=lambda _: SimpleNamespace(gnu_get_libc_version=libc),
                                  c_char_p=None),
    }
    namespace.update(overrides or {})
    exec(compile(source[begin:end], "installer-runtime-closure", "exec"), namespace)


@pytest.mark.parametrize("count", [2, 3])
def test_installer_accepts_declared_dependency_count_without_historical_count(tmp_path, count):
    builder = load("build-controller-release.py")
    fake_runtime(tmp_path, builder, count)
    run_installer_runtime_closure(tmp_path, builder)


@pytest.mark.parametrize("drift", ["missing", "hash", "identity", "count"])
def test_installer_keeps_exact_dependency_closure_guards(tmp_path, drift):
    builder = load("build-controller-release.py")
    fake_runtime(tmp_path, builder, 2)
    drift_runtime(tmp_path, builder, drift)
    with pytest.raises(RuntimeError):
        run_installer_runtime_closure(tmp_path, builder)


@pytest.mark.parametrize("requested,expected_exit", [("", 0), ("1.2.3", 0), ("9.9.9", 1), ("bad", 1)])
def test_linux_builder_reads_source_version_once_and_keeps_version_checks(tmp_path, requested, expected_exit):
    source = (ROOT / "deploy/linux/build-ubuntu-portable.sh").read_text()
    begin = source.index("done\n\n", source.index("for build_command")) + len("done\n\n")
    end = source.index("\nWORK_PARENT=", begin)
    fragment = source[begin:end]
    counter = tmp_path / "calls"
    counter.write_text("0\n")
    harness = (
        'VERSION="$1"; PROJECT_ROOT="$2"; counter="$3"\n'
        'python3() { local count=$(cat "$counter"); printf "%s\\n" "$((count+1))" >"$counter"; printf "1.2.3\\n"; }\n'
        'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n'
        + fragment + '\nprintf "%s\\n" "$VERSION"\n'
    )
    result = subprocess.run(["bash", "-c", harness, "version-test", requested, str(tmp_path), str(counter)],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert int(counter.read_text()) <= 1
    if expected_exit == 0:
        assert counter.read_text().strip() == "1"
        assert result.stdout.strip() == "1.2.3"


@pytest.mark.parametrize("fault", ["system", "machine", "implementation", "python", "glibc"])
def test_installer_rejects_incompatible_runtime_before_install(tmp_path, fault):
    builder = load("build-controller-release.py")
    fake_runtime(tmp_path, builder, 2)
    platform = SimpleNamespace(system=lambda: "Darwin" if fault == "system" else "Linux",
                               machine=lambda: "arm64" if fault == "machine" else "x86_64",
                               python_implementation=lambda: "PyPy" if fault == "implementation" else "CPython")
    class LibcVersion:
        restype = None
        def __call__(self): return b"2.34" if fault == "glibc" else b"2.35"
    libc = LibcVersion()
    overrides = {"platform": platform, "sys": SimpleNamespace(version_info=(3, 12) if fault == "python" else (3, 10)),
                 "ctypes": SimpleNamespace(CDLL=lambda _: SimpleNamespace(gnu_get_libc_version=libc), c_char_p=None)}
    with pytest.raises(RuntimeError, match="runtime requires"):
        run_installer_runtime_closure(tmp_path, builder, overrides)


@pytest.mark.parametrize("platform,payload,valid", [
    ("linux", fake_elf_header(), True),
    ("linux", fake_elf_header(machine=183), False),
    ("linux", fake_elf_header(elf_class=1), False),
    ("linux", fake_elf_header(byte_order=2), False),
    ("linux", b"text is not a binary", False),
    ("windows", fake_pe_header(), True),
    ("windows", fake_pe_header(machine=0xAA64), False),
    ("windows", fake_pe_header(optional_magic=0x10B), False),
    ("windows", fake_pe_header()[:65], False),
    ("windows", b"text is not a binary", False),
])
def test_static_main_binary_header_matches_declared_x86_64(platform, payload, valid):
    verifier = load("verify-release-artifacts.py")
    if valid:
        assert verifier.verify_binary_architecture(payload, platform) == hashlib.sha256(payload).hexdigest()
    else:
        with pytest.raises(RuntimeError): verifier.verify_binary_architecture(payload, platform)


@pytest.mark.parametrize("matched", [True, False])
def test_linux_pair_must_contain_identical_agent_bytes(tmp_path, monkeypatch, matched):
    verifier = load("verify-release-artifacts.py")
    calls = []
    def verify(path, version, online):
        calls.append((path, version, online))
        return "a" * 64 if not online or matched else "b" * 64
    monkeypatch.setattr(verifier, "verify_linux", verify)
    monkeypatch.setattr(sys, "argv", ["verify", "--output-dir", str(tmp_path), "--agent-version", "1.2.3", "--skip-windows", "--skip-controller"])
    if matched: assert verifier.main() == 0
    else:
        with pytest.raises(RuntimeError, match="binaries differ"): verifier.main()
    assert [v[2] for v in calls] == [False, True]


def windows_package(tmp_path, verifier, *, omit=None, extra=None):
    payloads = {name: b"fixture support\n" for name in verifier.WINDOWS_REQUIRED_PAYLOAD}
    payloads["RigPilotAgent.exe"] = fake_pe_header()
    if omit: payloads.pop(omit)
    if extra: payloads[extra] = b"untrusted name fixture\n"
    manifest = {"schema_version": 1, "version": "1.2.3", "platform": "windows", "architecture": "x86_64",
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in payloads.items()}}
    path = tmp_path / "windows.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in payloads.items(): archive.writestr("RigPilotAgent-Portable/" + name, content)
        archive.writestr("RigPilotAgent-Portable/agent-release.json", json.dumps(manifest))
    path.with_name(path.name + ".sha256").write_text(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    return path


@pytest.mark.parametrize("missing", ["RigPilotAgent.exe", "RigPilotAgentUpdater.ps1", "portable-agent.ps1", "Start-RigPilotAgent.bat"])
def test_windows_full_keeps_real_updater_minimum_payload(tmp_path, missing):
    verifier = load("verify-release-artifacts.py")
    with pytest.raises(RuntimeError, match="required update payload"):
        verifier.verify_windows(windows_package(tmp_path, verifier, omit=missing), "1.2.3")


@pytest.mark.parametrize("name,valid", [
    (name, False) for name in ["D:/outside", "file.txt:stream", "CON", "aux.txt", "COM1.txt",
        "folder./x", "folder /x", "a//b", "./a", "data/config.json", "versions/version.exe",
        "RigPilotAgent.exe/child", "rigpilotagent.exe/child",
        "a?b", "a*b", "a|b", "a<b", "a>b", 'a"b', "COM¹.txt", "LPT².log"]
] + [("😀" * 128 + ".txt", False), ("😀" * 100 + ".txt", True),
     ("測試/矿机.txt", True), ("café.txt", True)])
def test_windows_target_paths_are_not_posix_aliases(tmp_path, name, valid):
    verifier = load("verify-release-artifacts.py")
    path = windows_package(tmp_path, verifier, extra=name)
    if valid:
        verifier.verify_windows(path, "1.2.3")
    else:
        with pytest.raises(RuntimeError): verifier.verify_windows(path, "1.2.3")


def test_windows_smoke_refuses_drive_path_before_writing_or_executing(tmp_path, monkeypatch):
    verifier = load("verify-release-artifacts.py")
    archive = tmp_path / "escape.zip"
    with zipfile.ZipFile(archive, "w") as z: z.writestr("RigPilotAgent-Portable/D:/outside", b"untrusted")
    monkeypatch.setattr(verifier, "os", SimpleNamespace(name="nt"))
    original = tempfile.TemporaryDirectory
    monkeypatch.setattr(verifier.tempfile, "TemporaryDirectory", lambda **kwargs: original(dir=tmp_path, **kwargs))
    monkeypatch.setattr(verifier.subprocess, "run", lambda *a, **k: pytest.fail("must reject before execution"))
    with pytest.raises(RuntimeError, match="Windows release member"):
        verifier.smoke_windows(archive, "1.2.3")
    assert list(tmp_path.iterdir()) == [archive]


def linux_full_package(tmp_path, verifier, omit=None, version_payload=b"1.2.3\n"):
    root = "RigPilotAgent-Ubuntu-x64"
    payloads = {name: b"fixture support\n" for name in verifier.LINUX_FULL_REQUIRED_PAYLOAD
                if name not in {"agent-release.json", "SHA256SUMS"}}
    payloads["RigPilotAgent"] = fake_elf_header(); payloads["VERSION"] = version_payload
    if omit: payloads.pop(omit)
    manifest = {"schema_version": 1, "version": "1.2.3", "platform": "linux", "architecture": "x86_64",
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in payloads.items()}}
    payloads["agent-release.json"] = (json.dumps(manifest) + "\n").encode()
    payloads["SHA256SUMS"] = ''.join(hashlib.sha256(content).hexdigest() + '  ' + name + '\n' for name, content in payloads.items()).encode()
    records = [(root, b"", 0o755, tarfile.DIRTYPE)] + [
        (root + '/' + name, content, 0o755 if name in verifier.LINUX_FULL_EXECUTABLE_PAYLOAD else 0o644, tarfile.REGTYPE)
        for name, content in payloads.items()]
    return write_tar_fixture(tmp_path, records)


@pytest.mark.parametrize("version_payload", [b"1.2.3\n", b"9.9.9\n", b"1.2.3", b"1.2.3\nextra\n"])
def test_linux_full_minimum_is_installable_without_optional_documents(tmp_path, version_payload):
    verifier = load("verify-release-artifacts.py")
    assert not {"README-Ubuntu.txt", "BUILD-DEPENDENCIES.txt"}.intersection(verifier.LINUX_FULL_REQUIRED_PAYLOAD)
    path = linux_full_package(tmp_path, verifier, version_payload=version_payload)
    if version_payload == b"1.2.3\n":
        assert verifier.verify_linux(path, "1.2.3", online=False) == hashlib.sha256(fake_elf_header()).hexdigest()
    else:
        with pytest.raises(RuntimeError, match="VERSION"):
            verifier.verify_linux(path, "1.2.3", online=False)


@pytest.mark.parametrize("missing", ["Install.sh", "Start-Portable.sh", "Remove-Systemd.sh", "rigpilot-agent.service", "RigPilotAgentUpdater.py"])
def test_linux_full_missing_runtime_or_install_entry_is_rejected(tmp_path, missing):
    verifier = load("verify-release-artifacts.py")
    with pytest.raises(RuntimeError, match="required install payload"):
        verifier.verify_linux(linux_full_package(tmp_path, verifier, omit=missing), "1.2.3", online=False)


def test_verifier_resolves_shared_contract_from_any_cwd(tmp_path):
    (tmp_path / "release_contract.py").write_text("raise RuntimeError('untrusted cwd contract')\n")
    result = subprocess.run([sys.executable, str(ROOT / "deploy/verify-release-artifacts.py"), "--help"],
                            cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(tmp_path)}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--agent-version" in result.stdout and "--controller-version" in result.stdout


@pytest.mark.parametrize("fault", [None, "payload-hash", "sidecar", "missing-online", "different-binaries"])
def test_linux_leaf_verifies_completed_pair_before_success_without_retracting_files(tmp_path, fault):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    verifier = load("verify-release-artifacts.py")
    output = tmp_path / "output"
    output.mkdir()
    full_input = tmp_path / "full"
    full_input.mkdir()
    full = output / "RigPilotAgent-1.2.3-Ubuntu-x86_64.tar.gz"
    linux_full_package(full_input, verifier).rename(full)
    online = output / "RigPilotAgent-1.2.3-Ubuntu-x86_64-OnlineUpdate.tar.gz"
    if fault != "missing-online":
        online_input = tmp_path / "online"
        online_input.mkdir()
        root = "RigPilotAgent-Ubuntu-x64-OnlineUpdate"
        expected = fake_elf_header() + (b"different" if fault == "different-binaries" else b"")
        payload = expected + (b"corrupted" if fault == "payload-hash" else b"")
        manifest = {"schema_version": 1, "version": "1.2.3", "platform": "linux", "architecture": "x86_64",
                    "files": {"RigPilotAgent": hashlib.sha256(expected).hexdigest()}}
        write_tar_fixture(online_input, [
            (root, b"", 0o755, tarfile.DIRTYPE),
            (root + "/RigPilotAgent", payload, 0o755, tarfile.REGTYPE),
            (root + "/agent-release.json", (json.dumps(manifest) + "\n").encode(), 0o644, tarfile.REGTYPE),
        ]).rename(online)
    for archive in (full, online):
        if archive.exists():
            archive.with_name(archive.name + ".sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
    if fault == "sidecar":
        full.with_name(full.name + ".sha256").write_text("0" * 64 + "  " + full.name + "\n")
    before = {path.name: path.read_bytes() for path in output.iterdir()}
    source = (ROOT / "deploy/linux/build-ubuntu-portable.sh").read_text()
    # Execute the actual post-publication path with non-executable static ELF
    # fixtures; the build/installation/cleanup entry never runs.
    tail = source[source.index("# Verify both completed archives"):]
    harness = tmp_path / "leaf-completed-pair.sh"
    harness.write_text("set -Eeuo pipefail\n" +
        'PROJECT_ROOT="$1"; PYTHON="$2"; OUTPUT_DIR="$3"; VERSION="1.2.3"\n' +
        'ARCHIVE="$4"; ONLINE_ARCHIVE="$5"; ARCHIVE_SHA256="fixture"; ONLINE_SHA256="fixture"\n' + tail)
    result = subprocess.run([bash, str(harness), str(ROOT), sys.executable, str(output), str(full), str(online)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before
    assert (result.returncode == 0) is (fault is None), result.stdout + result.stderr
    assert ("RELEASE_VERIFICATION=ok" in result.stdout) is (fault is None)
    assert ("Manual package:" in result.stdout and "Online payload:" in result.stdout) is (fault is None)
