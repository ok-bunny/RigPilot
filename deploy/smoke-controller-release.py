#!/usr/bin/env python3
"""Verify a frozen Controller archive and run its HTTP scenarios offline.

This owner-operated Docker launcher uses an already qualified immutable image.
It never pulls images, publishes ports, installs on devices, or reads a live DB.
See CONTROLLER-HTTP-SMOKE.md for build/freeze/export composition.
"""
from __future__ import annotations

import argparse
import ast
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import stat
import subprocess
import sys
import tarfile
import uuid
import zipfile


HEX64 = re.compile(r"[0-9a-f]{64}")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
LOCK = re.compile(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.+!-]+) --hash=sha256:([0-9a-f]{64})")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def relative_path(value: str) -> str:
    require(isinstance(value, str), "path must be text")
    path = PurePosixPath(value)
    require(bool(value) and "\x00" not in value and "\\" not in value
            and not path.is_absolute() and ".." not in path.parts
            and path.as_posix() == value and value != ".", "unsafe release path")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON field")
        result[key] = value
    return result


def read_json(raw: bytes):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)


def project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def runtime_contract(files: dict[str, bytes], manifest: dict) -> tuple[dict, str]:
    """Derive closure from the actual payload, without release-count constants."""
    target = read_json(files["deploy/controller-runtime/runtime-target.json"])
    require(type(target.get("schema_version")) is int and target["schema_version"] == 1
            and target["controller_version"] == manifest["version"], "runtime identity differs")
    require(target["platform"]["system"] == "Linux" and target["platform"]["machine"] == "x86_64"
            and target["platform"]["glibc_minimum"] == "2.35", "unqualified runtime platform")
    require(target["python"]["implementation"] == "CPython"
            and target["python"]["major_minor"] == "3.10"
            and target["python"]["abi"] == "cp310"
            and re.fullmatch(r"3\.10\.[0-9]+", target["python"]["validated_patch"]), "unqualified Python runtime")
    dependencies = target["dependencies"]
    require(dependencies["only_binary"] is True and dependencies["require_hashes"] is True,
            "runtime must use binary wheels and required hashes")
    lock_path = relative_path(dependencies["lock"])
    wheelhouse = relative_path(dependencies["wheelhouse"])
    require(lock_path.startswith("deploy/controller-runtime/")
            and wheelhouse.startswith("deploy/controller-runtime/"), "runtime paths outside production closure")
    locked = {}
    for raw in files[lock_path].decode("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = LOCK.fullmatch(line)
        require(match is not None, "production lock must be exact and singly hashed")
        name, version, wanted = match.groups()
        name = project_name(name)
        require(name not in locked, "duplicate locked distribution")
        locked[name] = (version, wanted)
    wheels = {}
    for name, payload in files.items():
        if not name.startswith(wheelhouse + "/"):
            continue
        require(PurePosixPath(name).parent.as_posix() == wheelhouse and name.endswith(".whl"),
                "unexpected wheelhouse member")
        with zipfile.ZipFile(io.BytesIO(payload)) as wheel:
            metadata = [item for item in wheel.namelist()
                        if item.count("/") == 1 and item.endswith(".dist-info/METADATA")]
            require(len(metadata) == 1, "wheel METADATA set is invalid")
            parsed = BytesParser(policy=compat32).parsebytes(wheel.read(metadata[0]))
        require(parsed["Name"] and parsed["Version"], "wheel distribution identity missing")
        distribution = project_name(parsed["Name"])
        require(distribution not in wheels, "duplicate wheel distribution")
        wheels[distribution] = (parsed["Version"], hashlib.sha256(payload).hexdigest())
    require(bool(locked) and locked == wheels, "wheel identities and hashes differ from lock")
    require(type(dependencies["wheel_count"]) is int and dependencies["wheel_count"] == len(wheels),
            "runtime wheel count differs from actual closure")
    pins = {}
    for raw in files["server/requirements.txt"].decode("utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.+!-]+)", line)
        require(match is not None, "top-level requirements must be exact pins")
        name, version = match.groups()
        require(project_name(name) not in pins, "duplicate top-level requirement")
        pins[project_name(name)] = version
    require(bool(pins) and all(locked.get(name, (None,))[0] == version for name, version in pins.items()),
            "top-level requirements differ from production lock")
    tree = ast.parse(files["server/__init__.py"].decode("utf-8"), feature_version=(3, 10))
    versions = [ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(item, ast.Name) and item.id == "__version__" for item in node.targets)]
    require(versions == [manifest["version"]], "server version differs from manifest")
    tags = re.findall(r"[?&]v=([^\"'& >]+)", files["server/static/index.html"].decode("utf-8"))
    require(bool(tags), "HTML cache revision missing")
    cache = tags[0]
    require(cache == manifest["version"] or cache.startswith(manifest["version"] + "-"),
            "cache revision does not identify this release")
    for name in ("server/static/index.html",):
        current = re.findall(r"[?&]v=([^\"'& >]+)", files[name].decode("utf-8"))
        require(bool(current) and all(item == cache or item.startswith(cache + "-") for item in current),
                "HTML cache revisions disagree")
    return target, cache


def verified_archive(path: Path, wanted: str) -> tuple[dict[str, bytes], dict, dict, str]:
    require(path.is_file() and not path.is_symlink() and digest(path) == wanted,
            "archive is missing, unsafe, or differs from owner-frozen hash")
    sidecar = path.with_name(path.name + ".sha256")
    require(sidecar.is_file() and not sidecar.is_symlink()
            and sidecar.read_text(encoding="ascii") == wanted + "  " + path.name + "\n",
            "archive SHA-256 sidecar differs")
    files, directories, folded = {}, set(), set()
    total = 0
    with tarfile.open(path, "r:gz") as archive:
        for index, member in enumerate(archive, 1):
            require(index <= 4096, "archive exceeds resource entry limit")
            name = relative_path(member.name.rstrip("/"))
            require(name.casefold() not in folded, "duplicate archive path")
            folded.add(name.casefold())
            require((member.isdir() or member.isreg()) and 0 <= member.size <= 512 * 1024 * 1024
                    and not bool(getattr(member, "sparse", None)), "unsafe archive member")
            require(stat.S_IMODE(member.mode) == (0o755 if member.isdir() else 0o644),
                    "unexpected archive permissions")
            if member.isdir():
                require(member.size == 0, "directory contains data")
                directories.add(name)
                continue
            total += member.size
            require(total <= 2 * 1024 * 1024 * 1024, "archive exceeds expanded size limit")
            with archive.extractfile(member) as stream:
                payload = stream.read(member.size + 1)
            require(len(payload) == member.size, "truncated archive member")
            files[name] = payload
    manifest = read_json(files["controller-release.json"])
    require(isinstance(manifest, dict) and set(manifest) == {"schema_version", "version", "files"}
            and type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
            and isinstance(manifest["version"], str) and VERSION.fullmatch(manifest["version"]),
            "invalid Controller release manifest")
    hashes = manifest["files"]
    require(isinstance(hashes, dict) and bool(hashes), "manifest file map is invalid")
    for name, expected in hashes.items():
        relative_path(name)
        require(isinstance(expected, str) and HEX64.fullmatch(expected), "invalid payload SHA-256")
    require(set(files) == set(hashes) | {"controller-release.json"}, "archive members differ from manifest")
    parents = {parent.as_posix() for name in files for parent in PurePosixPath(name).parents
               if parent.as_posix() != "."}
    require(not (parents & set(files)) and directories.issubset(parents), "unsafe or unexpected archive directory")
    for name, expected in hashes.items():
        require(hashlib.sha256(files[name]).hexdigest() == expected, "payload hash differs: " + name)
        if name.endswith(".py"):
            ast.parse(files[name].decode("utf-8"), filename=name, feature_version=(3, 10))
    target, cache = runtime_contract(files, manifest)
    require(digest(path) == wanted, "archive changed during verification")
    return files, manifest, target, cache


def extract_or_verify(files: dict[str, bytes], root: Path):
    """Write only verified regular bytes; never delegate extraction to tar."""
    require(not root.is_symlink(), "extracted root must not be a link")
    if root.exists():
        require(root.is_dir(), "extracted root must be a directory")
        entries = list(root.rglob("*"))
        require(not any(path.is_symlink() for path in entries), "extracted tree contains a link")
        require({path.relative_to(root).as_posix() for path in entries if path.is_file()} == set(files),
                "existing extracted tree differs from archive")
        parents = {parent.as_posix() for name in files for parent in PurePosixPath(name).parents
                   if parent.as_posix() != "."}
        require({path.relative_to(root).as_posix() for path in entries if path.is_dir()}.issubset(parents)
                and all(path.is_file() or path.is_dir() for path in entries), "unexpected extracted object")
        for name, payload in files.items():
            require(digest(root / name) == hashlib.sha256(payload).hexdigest(), "extracted payload differs: " + name)
        return
    root.mkdir(mode=0o700)
    for name, payload in files.items():
        destination = root / name
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(payload)
        destination.chmod(0o644)


def cleanup_owned_container(name: str, label: str, token: str):
    record = {"name": name, "confirmed_stopped": False, "force_removed": False}
    def absent(result):
        return result.returncode != 0 and any(text in result.stderr.casefold()
                                              for text in ("no such container", "no such object"))
    try:
        result = subprocess.run(["docker", "inspect", "--type", "container", "--format",
                                 '{{.Id}} {{ index .Config.Labels "' + label + '" }}', name],
                                text=True, capture_output=True, timeout=15, check=False)
        if absent(result):
            return {**record, "status": "absent_after_run", "confirmed_stopped": True}
        if result.returncode:
            return {**record, "status": "inspection_failed", "error": result.stderr[-1000:]}
        fields = result.stdout.split()
        if len(fields) != 2 or HEX64.fullmatch(fields[0]) is None or fields[1] != token:
            return {**record, "status": "ownership_mismatch_not_removed"}
        record["container_id"] = fields[0]
        removed = subprocess.run(["docker", "rm", "--force", fields[0]], text=True,
                                 capture_output=True, timeout=15, check=False)
        record["force_removed"] = removed.returncode == 0
        remaining = subprocess.run(["docker", "inspect", "--type", "container", fields[0]],
                                   text=True, capture_output=True, timeout=15, check=False)
        if absent(remaining):
            return {**record, "status": "owned_container_removed", "confirmed_stopped": True}
        return {**record, "status": "removal_not_confirmed", "error": (removed.stderr + remaining.stderr)[-1000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**record, "status": "cleanup_failed", "error": str(exc)[:1000]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--scripts-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--smoke-script-sha256", required=True)
    parser.add_argument("--image-id", required=True, help="already qualified immutable sha256 image ID")
    parser.add_argument("--environment-note", required=True, help="actual VM/translation/platform qualification")
    parser.add_argument("--manifest-sha256", help="optional additional owner-frozen manifest pin")
    parser.add_argument("--main-sha256", help="optional additional owner-frozen main.py pin")
    args = parser.parse_args()
    for key in ("archive_sha256", "smoke_script_sha256", "manifest_sha256", "main_sha256"):
        value = getattr(args, key)
        if value is not None and HEX64.fullmatch(value) is None:
            parser.error(key + " must be an owner-frozen SHA-256")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", args.image_id) is None:
        parser.error("image-id must be the already qualified immutable image ID")
    files, manifest, target, cache = verified_archive(args.archive.absolute(), args.archive_sha256)
    manifest_sha = hashlib.sha256(files["controller-release.json"]).hexdigest()
    main_sha = manifest["files"]["server/main.py"]
    require(args.manifest_sha256 in (None, manifest_sha) and args.main_sha256 in (None, main_sha),
            "archive differs from additional owner-frozen pins")
    scripts = args.scripts_dir.absolute()
    require(not scripts.is_symlink() and scripts.is_dir(), "scripts directory is unsafe")
    scripts = scripts.resolve(strict=True)
    smoke = scripts / "smoke-controller-target.py"
    require(smoke.is_file() and not smoke.is_symlink() and digest(smoke) == args.smoke_script_sha256,
            "selected target script differs from frozen hash")
    work = args.work_root.absolute()
    require(not work.is_symlink(), "work root must not be a link")
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = work.resolve(strict=True)
    source, output = work / "extracted", work / "smoke-output"
    extract_or_verify(files, source)
    require(not output.exists() and not output.is_symlink(), "use a fresh smoke-output directory")
    output.mkdir(mode=0o700)
    require(all("," not in str(path) and "\n" not in str(path) for path in (source, scripts, output)),
            "Docker bind paths must not contain comma or newline")
    smoke_args = ["/output/venv/bin/python", "-B", "/scripts/smoke-controller-target.py",
                  "--manifest-sha256", manifest_sha, "--main-sha256", main_sha,
                  "--cache-version", cache, "--environment-note", args.environment_note]
    dependencies = target["dependencies"]
    command = ("set -eu; python3 -m venv /output/venv; " + shlex.join([
        "/output/venv/bin/pip", "install", "--no-index", "--only-binary=:all:", "--require-hashes",
        "--find-links=/input/" + dependencies["wheelhouse"], "-r", "/input/" + dependencies["lock"]])
        + " >/output/offline-pip.log 2>&1; " + shlex.join(smoke_args))
    token, label = uuid.uuid4().hex, "rigpilot.controller.smoke-owner"
    name = "rigpilot-controller-smoke-" + token
    argv = ["docker", "run", "--rm", "--pull", "never", "--name", name, "--label", label + "=" + token,
            "--network", "none", "--platform", "linux/amd64", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--env", "PIP_NO_INDEX=1", "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--user", "root", "--cpus", "2", "--memory", "2g",
            "--mount", "type=bind,src=" + str(source) + ",dst=/input,readonly",
            "--mount", "type=bind,src=" + str(scripts) + ",dst=/scripts,readonly",
            "--mount", "type=bind,src=" + str(output) + ",dst=/output",
            args.image_id, "/bin/sh", "-c", command]
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt("native smoke launcher interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    launch_error, exit_code = None, 1
    try:
        with (output / "container.stdout.log").open("xb") as stdout, (output / "container.stderr.log").open("xb") as stderr:
            result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                    timeout=300, check=False)
            exit_code = result.returncode
    except BaseException as exc:
        launch_error = type(exc).__name__ + ": " + str(exc)[:1000]
        exit_code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        cleanup = cleanup_owned_container(name, label, token)
    report, report_error = None, None
    try:
        report = read_json((output / "report.json").read_bytes())
        require(isinstance(report, dict), "native report must be an object")
    except (OSError, ValueError, RuntimeError) as exc:
        report_error = type(exc).__name__ + ": " + str(exc)[:500]
    passed = (exit_code == 0 and isinstance(report, dict) and report.get("status") == "passed"
              and report.get("uvicorn_stopped") is True and cleanup["confirmed_stopped"]
              and report.get("manifest_sha256") == manifest_sha and report.get("main_sha256") == main_sha
              and report.get("cache_version") == cache and report.get("smoke_script_sha256") == args.smoke_script_sha256)
    record = {"schema": "rigpilot.controller.target-native-smoke-launch.v1", "script_key": "controller-native-api",
              "status": "passed" if passed else "failed", "exit_code": exit_code,
              "image_id": args.image_id, "network": "none", "work_root": str(work),
              "archive_sha256": args.archive_sha256, "source_manifest_sha256": manifest_sha,
              "controller_version": manifest["version"], "main_sha256": main_sha, "cache_version": cache,
              "python_script_sha256": args.smoke_script_sha256, "launcher_script_sha256": digest(Path(__file__)),
              "launch_error": launch_error, "report_error": report_error, "container_cleanup": cleanup}
    with (output / "result.json").open("x") as stream:
        stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
