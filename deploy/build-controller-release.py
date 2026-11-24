"""Build one deterministic, allowlisted RigPilot Controller release archive."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import compat32
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_FILES = (
    "deploy/controller-runtime/requirements-controller-linux-py310.lock",
    "deploy/controller-runtime/runtime-target.json",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/annotated_doc-0.0.5-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/annotated_types-0.8.0-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/anyio-4.14.2-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/click-8.4.2-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/exceptiongroup-1.3.1-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/fastapi-0.139.0-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/h11-0.16.0-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/httptools-0.8.0-cp310-cp310-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/idna-3.18-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/pydantic-2.13.4-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/pydantic_core-2.46.4-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/python_dotenv-1.2.2-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/python_multipart-0.0.32-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/pyyaml-6.0.3-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/starlette-1.6.0-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/typing_extensions-4.16.0-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/typing_inspection-0.4.4-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/uvicorn-0.51.0-py3-none-any.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/uvloop-0.22.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/watchfiles-1.2.0-cp310-cp310-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
    "deploy/controller-runtime/wheelhouse/linux-x86_64-py310/websockets-16.1.1-cp310-cp310-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl",
    "server/__init__.py",
    "server/auth.py",
    "server/config.py",
    "server/database.py",
    "server/flight_v2_storage.py",
    "server/main.py",
    "server/managed_miners.py",
    "server/miner_adapters/__init__.py",
    "server/miner_adapters/builtin.py",
    "server/miner_adapters/contract.py",
    "server/miner_adapters/retired.py",
    "server/miner_adapters/templates.py",
    "server/miner_adapters/runtime_v1.py",
    "server/miner_adapters/seine_catalog.py",
    "server/miner_adapters/srb_catalog.py",
    "server/miner_adapters/validation.py",
    "server/miner_adapters/workload.py",
    "server/pci_identity.py",
    "server/requirements.txt",
    "server/schemas.py",
    "server/schemas_v2.py",
    "server/static/.keep",
    "server/static/favicon.svg",
    "server/static/app.js",
    "server/static/index.html",
    "server/static/pci-identity-data.js",
    "server/static/styles.css",
    "server/static/theme-init.js",
    "server/system_metrics.py",
    "server/workload.py",
)
VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
)
VERSION_RE = re.compile(
    r'^__version__\s*=\s*["\x27](' + VERSION.pattern + r')["\x27]\s*$',
    re.MULTILINE,
)
PIN_RE = re.compile(
    r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_.,-]+\])?==[A-Za-z0-9_.+!-]+$"
)
LOCK_RE = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.+!-]+) "
    r"--hash=sha256:([0-9a-f]{64})$"
)
RUNTIME_ROOT = "deploy/controller-runtime"
WHEELHOUSE = f"{RUNTIME_ROOT}/wheelhouse/linux-x86_64-py310"


def _warn(message: str) -> None:
    try:
        print(f"WARNING: {message}", file=sys.stderr)
    except Exception:
        # Diagnostics must not replace the original publication/cleanup error.
        pass


def _fsync_published_directory(path: Path) -> None:
    if os.name != "posix":
        _warn("output directory fsync is unavailable on this platform; publication durability is unconfirmed")
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError as exc:
            _warn(f"output directory descriptor cleanup failed: {exc}")
        raise
    else:
        os.close(descriptor)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_version() -> str:
    source = (PROJECT_ROOT / "server" / "__init__.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(source)
    if match is None:
        raise RuntimeError("cannot read controller source version")
    return match.group(1)


def normalized_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_names = [
                name
                for name in wheel.namelist()
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise RuntimeError(
                    f"controller wheel METADATA set is invalid: {path.name}"
                )
            metadata = BytesParser(policy=compat32).parsebytes(
                wheel.read(metadata_names[0])
            )
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"controller wheel is invalid: {path.name}") from exc
    if not metadata["Name"] or not metadata["Version"]:
        raise RuntimeError(f"controller wheel identity is missing: {path.name}")
    return normalized_project_name(metadata["Name"]), metadata["Version"]


def validate_runtime_closure(version: str) -> None:
    expected_wheels = {
        PurePosixPath(name).name
        for name in ALLOWED_FILES
        if name.startswith(f"{WHEELHOUSE}/")
    }
    target = json.loads(
        (PROJECT_ROOT / RUNTIME_ROOT / "runtime-target.json").read_text(
            encoding="utf-8"
        )
    )
    expected_target = {
        "schema_version": 1,
        "controller_version": version,
        "platform": {
            "system": "Linux",
            "machine": "x86_64",
            "glibc_minimum": "2.35",
        },
        "python": {
            "implementation": "CPython",
            "major_minor": "3.10",
            "validated_patch": "3.10.12",
            "abi": "cp310",
        },
        "dependencies": {
            "lock": f"{RUNTIME_ROOT}/requirements-controller-linux-py310.lock",
            "wheelhouse": WHEELHOUSE,
            "wheel_count": len(expected_wheels),
            "only_binary": True,
            "require_hashes": True,
        },
    }
    if (
        target != expected_target
        or type(target["schema_version"]) is not int
        or type(target["dependencies"]["wheel_count"]) is not int
        or target["dependencies"]["only_binary"] is not True
        or target["dependencies"]["require_hashes"] is not True
    ):
        raise RuntimeError("controller runtime target is invalid")

    locked: dict[str, tuple[str, str]] = {}
    lock_path = PROJECT_ROOT / target["dependencies"]["lock"]
    for raw in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = LOCK_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(
                "controller production lock is not exact and singly hashed"
            )
        name, locked_version, digest = match.groups()
        key = normalized_project_name(name)
        if key in locked:
            raise RuntimeError(f"duplicate controller production lock entry: {name}")
        locked[key] = (locked_version, digest)

    development_pins: dict[str, str] = {}
    requirements = PROJECT_ROOT / "server" / "requirements.txt"
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if PIN_RE.fullmatch(line) is None:
            raise RuntimeError("controller requirements must contain only exact pins")
        name, pinned_version = line.split("==", 1)
        key = normalized_project_name(name.split("[", 1)[0])
        development_pins[key] = pinned_version
    if not development_pins:
        raise RuntimeError("controller requirements must contain only exact pins")
    if any(
        locked.get(name, ("", ""))[0] != pinned
        for name, pinned in development_pins.items()
    ):
        raise RuntimeError(
            "controller production lock does not include development top-level pins"
        )

    wheel_files = {
        path.name: path
        for path in (PROJECT_ROOT / WHEELHOUSE).glob("*.whl")
        if path.is_file() and not path.is_symlink()
    }
    if set(wheel_files) != expected_wheels:
        raise RuntimeError(
            "controller wheelhouse file set does not match the release allowlist"
        )
    wheels: dict[str, tuple[str, str]] = {}
    for path in wheel_files.values():
        name, wheel_version = wheel_identity(path)
        if name in wheels:
            raise RuntimeError(f"duplicate controller wheel distribution: {name}")
        wheels[name] = (wheel_version, sha256(path))
    if wheels != locked:
        raise RuntimeError(
            "controller wheelhouse identities and hashes do not match the production lock"
        )


def validate_sources(version: str) -> dict[str, str]:
    actual = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for root in (PROJECT_ROOT / "server", PROJECT_ROOT / RUNTIME_ROOT)
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink())
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        # Finder metadata is not a release input. Keep rejecting unknown code
        # and metadata-named links rather than ignoring all hidden files.
        and not (path.name == ".DS_Store" and not path.is_symlink())
    }
    expected = set(ALLOWED_FILES)
    if actual != expected:
        added = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise RuntimeError(f"controller release allowlist drift; added={added}, missing={missing}")
    if source_version() != version:
        raise RuntimeError("requested version does not match controller source")

    hashes: dict[str, str] = {}
    root_resolved = PROJECT_ROOT.resolve(strict=True)
    for relative in ALLOWED_FILES:
        path = PROJECT_ROOT / PurePosixPath(relative)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"controller source is missing or unsafe: {relative}")
        resolved = path.resolve(strict=True)
        if root_resolved not in resolved.parents:
            raise RuntimeError(f"controller source escapes the project: {relative}")
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8"), relative, "exec")
        hashes[relative] = sha256(path)
    validate_runtime_closure(version)
    return hashes


def tar_info(name: str, *, directory: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name + ("/" if directory else ""))
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = 0o755 if directory else 0o644
    if directory:
        info.type = tarfile.DIRTYPE
    return info


def build(
    version: str,
    output_dir: Path,
) -> tuple[Path, Path, str]:
    if VERSION.fullmatch(version) is None:
        raise RuntimeError("version must be X.Y.Z")
    hashes = validate_sources(version)
    manifest = {
        "schema_version": 1,
        "version": version,
        "files": hashes,
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"RigPilotController-{version}.tar.gz"
    sidecar = target.with_name(target.name + ".sha256")
    if target.exists() or target.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise RuntimeError(f"refusing to overwrite an existing release: {target}")

    directories: set[str] = set()
    for relative in ALLOWED_FILES:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            parent = parent.parent

    temporary: Path | None = None
    sidecar_temporary: Path | None = None
    target_published = False
    sidecar_published = False
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".part", dir=output_dir, delete=False
        ) as raw:
            temporary = Path(raw.name)
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                    for directory in sorted(directories):
                        archive.addfile(tar_info(directory, directory=True))
                    for relative in ALLOWED_FILES:
                        path = PROJECT_ROOT / PurePosixPath(relative)
                        payload = path.read_bytes()
                        if hashlib.sha256(payload).hexdigest() != hashes[relative]:
                            raise RuntimeError(f"controller source changed while building: {relative}")
                        info = tar_info(relative)
                        info.size = len(payload)
                        archive.addfile(info, io.BytesIO(payload))
                    manifest_info = tar_info("controller-release.json")
                    manifest_info.size = len(manifest_bytes)
                    archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
            raw.flush()
            os.fsync(raw.fileno())
        digest = sha256(temporary)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            newline="\n",
            prefix=f".{sidecar.name}.",
            suffix=".part",
            dir=output_dir,
            delete=False,
        ) as hash_file:
            sidecar_temporary = Path(hash_file.name)
            hash_file.write(f"{digest}  {target.name}\n")
            hash_file.flush()
            os.fsync(hash_file.fileno())
        # Complete both private files before publishing either staged inode.
        # Both links stay on one filesystem and never overwrite another writer.
        os.link(temporary, target)
        target_published = True
        os.link(sidecar_temporary, sidecar)
        sidecar_published = True
        _fsync_published_directory(output_dir)
        return target, sidecar, digest
    except BaseException:
        if sidecar_published:
            _warn(f"published-but-durability-unconfirmed; inspect public paths left untouched: {target}, {sidecar}")
        elif target_published:
            # There is no atomic "unlink only this inode" for a shared name.
            # Keep public paths: another writer may already have replaced them.
            _warn(f"partial publication; inspect public paths left untouched: {target}, {sidecar}")
        raise
    finally:
        for private in (temporary, sidecar_temporary):
            if private is None:
                continue
            try:
                private.unlink(missing_ok=True)
            except OSError as exc:
                _warn(f"private temporary cleanup failed for {private}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs")
    args = parser.parse_args()
    version = args.version or source_version()
    if VERSION.fullmatch(version) is None:
        parser.error("version must be X.Y.Z")
    target, sidecar, digest = build(version, args.output_dir.resolve())
    print(f"CONTROLLER_ARCHIVE={target}")
    print(f"CONTROLLER_SHA256={digest}")
    print(f"CONTROLLER_SHA256_FILE={sidecar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
