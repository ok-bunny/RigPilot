"""Run the builder's real preflight code without block devices or guest boot."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile

import pytest


SCRIPT = Path(__file__).parents[1] / "build-rigpilotos-target.sh"
VERSION = "0.8.22"
PREFIX = "RigPilotAgent-Ubuntu-x64"


def embedded(function):
    source = SCRIPT.read_text(encoding="utf-8")
    code = source.split(f"{function}() {{\n", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    scope = {"__name__": "preflight_tests"}
    exec(compile(code, str(SCRIPT), "exec"), scope)
    return scope


def test_generated_grub_rejects_tab_nbd_root_even_with_valid_pci_entry():
    root_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    text = ("\tlinux\t/boot/vmlinuz root=/dev/nbd0p4 ro console=tty0\n"
            f"    linux /boot/vmlinuz root=UUID={root_uuid} ro pci=realloc=on\n")
    with pytest.raises(ValueError, match="GRUB line 1"):
        embedded("validate_generated_grub")["check_grub_cfg"](text, root_uuid)


def test_generated_grub_rejects_duplicate_root_in_linuxefi_entry():
    root_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    text = f"linuxefi\t/boot/vmlinuz root=UUID={root_uuid} root=/dev/nbd0p4 ro\n"
    with pytest.raises(ValueError, match="requires exactly"):
        embedded("validate_generated_grub")["check_grub_cfg"](text, root_uuid)


def test_generated_grub_accepts_all_normal_advanced_recovery_and_pci_uuid_entries():
    root_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    text = "\n".join(f"{command}{separator}/boot/vmlinuz root=UUID={root_uuid} ro {args}"
                     for command, separator, args in (
                         ("linux", "\t", "quiet"),
                         ("linux", "\t", "quiet"),
                         ("linuxefi", "\t", "recovery nomodeset"),
                         ("linux", " ", "pci=realloc=on"),
                         ("linux", " ", "pci=realloc=on,nocrs")))
    result = embedded("validate_generated_grub")["check_grub_cfg"](text, root_uuid)
    assert result["linux_entries"] == 5
    assert {entry["root"] for entry in result["entries"]} == {"root=UUID=" + root_uuid}


def device(number, kind="disk", *, children=(), mounts=None):
    return {"name": "/dev/test", "maj:min": number, "type": kind,
            "mountpoints": [mounts], "children": list(children)}


def target_check(tmp_path, *, system=None, target=None, missing_holders=False):
    scope = embedded("verify_target_device")
    if system is None:
        system = device("253:0", "lvm", children=[device("9:0", "raid1", children=[
            device("8:2", "part", children=[device("8:0")]),
            device("8:18", "part", children=[device("8:16")]),
        ])])
    if target is None:
        target = device("43:0", children=[device("43:1", "part")])

    def query(*args):
        if args[0] == "findmnt":
            return "253:0"
        return json.dumps({"blockdevices": [system if "--inverse" in args else target]})

    scope["query"] = query
    pending = [target]
    while pending:
        node = pending.pop()
        if not missing_holders:
            (tmp_path / node["maj:min"] / "holders").mkdir(parents=True, exist_ok=True)
        pending.extend(node["children"])
    return lambda: scope["verify_target"]("/dev/nbd0", sysfs=tmp_path)


def test_nested_lvm_raid_all_system_disk_ancestors_are_protected(tmp_path):
    for number in ("8:0", "8:16"):
        with pytest.raises(SystemExit, match="builder system disk"):
            target_check(tmp_path, target=device(number))()


def test_disposable_disk_with_idle_partitions_passes(tmp_path):
    target_check(tmp_path)()


def test_active_holder_on_unmounted_partition_is_rejected(tmp_path):
    check = target_check(tmp_path)
    (tmp_path / "43:1" / "holders" / "dm-5").touch()
    with pytest.raises(SystemExit, match="holders"):
        check()


@pytest.mark.parametrize("mount", ["/mnt/data", "[SWAP]"])
def test_mounted_or_swap_descendant_is_rejected(tmp_path, mount):
    check = target_check(tmp_path, target=device("43:0", children=[device("43:1", "part", mounts=mount)]))
    with pytest.raises(SystemExit, match="mounted or active swap"):
        check()


def test_unknown_holder_state_does_not_pass(tmp_path):
    with pytest.raises(FileNotFoundError):
        target_check(tmp_path, missing_holders=True)()


def test_unresolved_system_disk_ancestry_does_not_pass(tmp_path):
    with pytest.raises(SystemExit, match="system disk ancestry"):
        target_check(tmp_path, system=device("253:0", "lvm"))()


def package_payload():
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", header, 18, 62)
    files = {name: b"# package fixture\n" for name in (
        "Install.sh", "Remove-Systemd.sh", "RigPilotAgentLauncher.sh",
        "RigPilotAgentUpdater.py", "Start-Portable.sh", "rigpilot-agent.service",
        "rigpilot-agent-supervision.conf",
        "rigpilot-agent-update-recovery.service", "rigpilot-agent-update-recovery.timer",
        "docs/nested/README.txt",
    )}
    files.update({"RigPilotAgent": bytes(header), "VERSION": (VERSION + "\n").encode()})
    return files


def with_manifest(files):
    manifest = {"schema_version": 1, "version": VERSION, "platform": "linux",
                "architecture": "x86_64", "files": {
                    name: hashlib.sha256(value).hexdigest() for name, value in files.items()
                }}
    files["agent-release.json"] = json.dumps(manifest).encode() + b"\n"
    return files


def with_sums(files):
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(value).hexdigest()}  {name}\n"
                                 for name, value in files.items() if name != "SHA256SUMS").encode()
    return files


def archive_at(tmp_path, files, *, extras=()):
    path = tmp_path / "agent.tar.gz"
    directories = {PREFIX}
    for name in files:
        directories.update(str(parent) for parent in (Path(PREFIX) / name).parents if str(parent) != ".")
    with tarfile.open(path, "w:gz") as archive:
        for name in sorted(directories):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, data in files.items():
            member = tarfile.TarInfo(f"{PREFIX}/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        for member in extras:
            archive.addfile(member)
    return path


def validate(path):
    embedded("validate_agent_package")["validate_package"](path, VERSION)


def test_complete_package_with_nested_payload_and_directories_passes(tmp_path):
    validate(archive_at(tmp_path, with_sums(with_manifest(package_payload()))))


def test_unlisted_payload_in_manifest_is_rejected_even_with_valid_sums(tmp_path):
    files = with_manifest(package_payload())
    files["unlisted.sh"] = b"unlisted payload\n"
    with pytest.raises(SystemExit, match="manifest file set"):
        validate(archive_at(tmp_path, with_sums(files)))


def test_manifest_cannot_hash_different_payload_despite_valid_sums(tmp_path):
    files = with_manifest(package_payload())
    files["Install.sh"] += b"changed\n"
    with pytest.raises(SystemExit, match="manifest hash mismatch"):
        validate(archive_at(tmp_path, with_sums(files)))


@pytest.mark.parametrize("mutation", ["omit", "duplicate", "incorrect"])
def test_checksum_file_requires_exact_coverage(tmp_path, mutation):
    files = with_sums(with_manifest(package_payload()))
    lines = files["SHA256SUMS"].splitlines(keepends=True)
    if mutation == "omit":
        lines = lines[1:]
    elif mutation == "duplicate":
        lines.append(lines[0])
    else:
        lines[0] = b"0" * 64 + lines[0][64:]
    files["SHA256SUMS"] = b"".join(lines)
    with pytest.raises(SystemExit, match="SHA256SUMS"):
        validate(archive_at(tmp_path, files))


@pytest.mark.parametrize("field,value", [("version", "0.0.1"), ("architecture", "aarch64"), ("schema_version", True)])
def test_manifest_identity_must_match_even_with_valid_sums(tmp_path, field, value):
    files = with_manifest(package_payload())
    manifest = json.loads(files["agent-release.json"])
    manifest[field] = value
    files["agent-release.json"] = json.dumps(manifest).encode() + b"\n"
    with pytest.raises(SystemExit, match="manifest identity"):
        validate(archive_at(tmp_path, with_sums(files)))


def test_duplicate_manifest_fields_are_rejected(tmp_path):
    files = with_manifest(package_payload())
    files["agent-release.json"] = files["agent-release.json"].replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')
    with pytest.raises(SystemExit, match="duplicate Agent manifest"):
        validate(archive_at(tmp_path, with_sums(files)))


def test_required_executable_cannot_be_replaced_with_directory(tmp_path):
    files = package_payload()
    del files["Install.sh"]
    directory = tarfile.TarInfo(f"{PREFIX}/Install.sh")
    directory.type = tarfile.DIRTYPE
    with pytest.raises(SystemExit, match="incomplete"):
        validate(archive_at(tmp_path, with_sums(with_manifest(files)), extras=[directory]))


def test_package_preflight_happens_before_erasure_and_again_before_extraction():
    source = SCRIPT.read_text(encoding="utf-8")
    erase = source.index('wipefs --all --force')
    assert source.index('\nvalidate_agent_package\n') < erase
    assert source.index('\nverify_target_device\n') < erase
    assert erase < source.rindex('\nvalidate_agent_package\n') < source.index('tar --extract --gzip')


@pytest.mark.parametrize("name", [
    "rigpilot-agent.service",
    "rigpilot-agent-supervision.conf", "rigpilot-agent-update-recovery.service",
    "rigpilot-agent-update-recovery.timer",
])
def test_missing_runtime_policy_is_rejected_with_internally_consistent_hashes(tmp_path, name):
    files = package_payload()
    del files[name]
    with pytest.raises(SystemExit, match="incomplete"):
        validate(archive_at(tmp_path, with_sums(with_manifest(files))))


@pytest.mark.parametrize("name", [
    "Setup-HiveOS-Runtime.sh", "rigpilot-agent-remote-sessions.conf",
])
def test_retired_hiveos_member_is_rejected_with_consistent_manifest_and_checksums(tmp_path, name):
    files = package_payload()
    files[name] = b"# retired member fixture\n"
    with pytest.raises(SystemExit, match="retired Agent package member"):
        validate(archive_at(tmp_path, with_sums(with_manifest(files))))


def test_wifi_policy_member_is_rejected_with_consistent_manifest_and_checksums(tmp_path):
    files = package_payload()
    files["rigpilot-agent-wifi.conf"] = b"[Service]\n"
    with pytest.raises(SystemExit, match="Wi-Fi Agent package is not permitted"):
        validate(archive_at(tmp_path, with_sums(with_manifest(files))))


def test_generic_release_verifier_rejects_duplicate_checksum_entries(tmp_path):
    import importlib.util
    source = SCRIPT.parents[1] / "verify-release-artifacts.py"
    spec = importlib.util.spec_from_file_location("release_verifier_preflight_tests", source)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    files = with_sums(with_manifest(package_payload()))
    files["SHA256SUMS"] += files["SHA256SUMS"].splitlines(keepends=True)[0]
    path = archive_at(tmp_path, files)
    regular = {f"{PREFIX}/{name}": value for name, value in files.items()}
    executable = {"Install.sh", "Remove-Systemd.sh", "RigPilotAgent", "RigPilotAgentLauncher.sh",
                  "RigPilotAgentUpdater.py", "Start-Portable.sh"}
    modes = {f"{PREFIX}/{name}": 0o755 if name in executable else 0o644 for name in files}
    verifier.verify_sidecar = lambda _path: None
    verifier.regular_tar_members = lambda _path: (regular, set(), modes)
    with pytest.raises(RuntimeError, match="duplicate paths"):
        verifier.verify_linux(path, VERSION, online=False)


# Removed with 0.2.12: the NVIDIA driver no longer installs through an apt
# transaction (CUDA repo rotated the 615 branch out), so the apt-simulation
# guard against NVIDIA-driven package removal has no transaction to guard.
# The run-installer equivalent gates live in test_nvidia_pinning.py.


@pytest.mark.parametrize("text_member", ["Install.sh", "docs/nested/README.txt"])
@pytest.mark.parametrize("bad_text", [b"text with NUL\x00\n", b"CRLF text\r\n"])
def test_agent_text_policy_rejects_invalid_bytes_despite_consistent_hashes(
    tmp_path, text_member, bad_text,
):
    files = package_payload()
    files[text_member] = bad_text
    with pytest.raises(SystemExit, match="Agent text member must be NUL-free LF text"):
        validate(archive_at(tmp_path, with_sums(with_manifest(files))))


@pytest.mark.parametrize("mutation", ["truncated", "ident-version"])
def test_agent_elf_header_rejects_invalid_identity_with_consistent_hashes(tmp_path, mutation):
    files = package_payload()
    header = bytearray(files["RigPilotAgent"])
    if mutation == "truncated":
        header = header[:20]
    else:
        header[6] = 0
    files["RigPilotAgent"] = bytes(header)
    with pytest.raises(SystemExit, match="ELF64"):
        validate(archive_at(tmp_path, with_sums(with_manifest(files))))


def cleanliness_fragment(stage):
    source = SCRIPT.read_text()
    if stage == "bytecode":
        start = source.index('fail "SSH policy mode or owner is invalid"')
        start = source.index("\n", start)
        end = source.index("DEFAULT_KERNEL_PATH=", start)
    elif stage == "ssh-cleanup":
        start = source.index('rm -f -- "${ROOT_MOUNT}"/etc/ssh/ssh_host_*_key',
                             source.index("# Never clone SSH host identity"))
        end = source.index('cat >"${CONFIG_MOUNT}/README.txt"', start)
    else:
        start = source.index("for forbidden_path in ", source.index("# The release master must not carry"))
        end = source.index("# Refuse to publish", start)
    return source[start:end]


@pytest.mark.parametrize("stage,directory,optional", [
    ("bytecode", "usr/libexec", False), ("ssh-cleanup", "etc/ssh", False),
    ("factory", "etc/ssh", False), ("factory", "var/lib/rigpilot-agent", True),
    ("factory", "etc/rigpilot-agent", True),
])
@pytest.mark.parametrize("case", ["clean", "missing", "not-directory", "dangling-link", "scan-error"])
def test_cleanliness_scans_require_readable_directories(tmp_path, stage, directory, optional, case):
    import subprocess

    root, provision = tmp_path / "root", tmp_path / "provision"
    provision.mkdir()
    for relative in ("usr/libexec", "etc/ssh", "var/lib/rigpilot-agent", "etc/rigpilot-agent"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    target = root / directory
    if case in ("missing", "not-directory", "dangling-link"):
        target.rmdir()
        if case == "not-directory":
            target.write_text("not a directory")
        elif case == "dangling-link":
            target.symlink_to(tmp_path / "absent")
    harness = 'set -Eeuo pipefail\nROOT_MOUNT="$1"; CONFIG_MOUNT="$2"; FAILED_SCAN="$3"\n'
    harness += 'fail() { printf "%s\\n" "$*" >&2; exit 1; }\n'
    if case == "scan-error":
        harness += 'find() { if [[ "$1" == "$FAILED_SCAN" ]]; then return 71; fi; command find "$@"; }\n'
    result = subprocess.run(["bash", "-c", harness + cleanliness_fragment(stage) + "\necho CLEAN_SCAN_PASSED\n",
                             "scan-fixture", str(root), str(provision), str(target)],
                            text=True, capture_output=True, timeout=5)
    accepted = case == "clean" or (case == "missing" and optional)
    assert (result.returncode == 0) is accepted, (stage, directory, case, result.stderr)
    assert ("CLEAN_SCAN_PASSED" in result.stdout) is accepted
    if case == "not-directory":
        assert target.read_text() == "not a directory"
    elif case == "dangling-link":
        assert target.is_symlink()
