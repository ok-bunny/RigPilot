#!/usr/bin/env bash
set -Eeuo pipefail

mode=${1:?mode is required: prepare, commit, abort, or finalize}
case "$mode" in
    prepare)
        prepared_state=""
        archive=${2:?archive path is required}
        expected_sha256=${3:?SHA-256 is required}
        version=${4:?version is required}
        ;;
    commit|abort|finalize)
        prepared_state=${2:?prepared state directory is required}
        archive=${3:?archive path is required}
        expected_sha256=${4:?SHA-256 is required}
        version=${5:?version is required}
        ;;
    *)
        printf 'Controller deployment refused: unsupported mode: %s\n' "$mode" >&2
        exit 1
        ;;
esac
service=${RIGPILOT_SERVICE:-rigpilot-controller}
install_root=${RIGPILOT_INSTALL_ROOT:-/opt/rigpilot}
# Match the selected service's listener while keeping health checks on loopback.
health_port=${RIGPILOT_CONTROLLER_HEALTH_PORT:-8080}
# Seconds the commit phase waits, probing health once per second, for the
# newly started Controller to report healthy before rolling back.  The
# default of 30 preserves the historical window for compatibility;
# releases that migrate the live database during startup can exceed it
# (0.8.50 measured 89.68s), so deploy such versions with
# RIGPILOT_HEALTH_WAIT_SECONDS >= 180.
health_wait_seconds=${RIGPILOT_HEALTH_WAIT_SECONDS:-30}
readonly lock_file="/run/lock/rigpilot-controller-deploy.lock"
readonly prepare_ttl_seconds=7200
lock_fd=${RIGPILOT_DEPLOY_LOCK_FD:-}
lock_owner_pid=${RIGPILOT_DEPLOY_LOCK_OWNER_PID:-}

fail() {
    printf 'Controller deployment refused: %s\n' "$*" >&2
    exit 1
}

[[ "$health_port" =~ ^[1-9][0-9]{0,4}$ ]] && ((health_port <= 65535)) \
    || fail "Controller health port must be an integer from 1 to 65535"
[[ "$health_wait_seconds" =~ ^[1-9][0-9]{0,5}$ ]] && ((health_wait_seconds <= 86400)) \
    || fail "Controller health wait window must be an integer from 1 to 86400 seconds"

for required in flock python3 sha256sum systemctl curl realpath; do
    command -v "$required" >/dev/null 2>&1 || fail "required command is unavailable: $required"
done
[[ "$EUID" -eq 0 ]] || fail "run this deployment as root"
[[ "$(uname -s)" == "Linux" ]] || fail "only Linux Controller hosts are supported"
[[ "$(uname -m)" == "x86_64" ]] || fail "only Linux x86_64 Controller hosts are supported"
[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    || fail "version must be canonical X.Y.Z"
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || fail "SHA-256 must be 64 lowercase hexadecimal characters"
[[ "$install_root" == /* && "$install_root" != "/" ]] || fail "install root must be a non-root absolute path"
[[ "$install_root" != *$'\n'* && "$install_root" != *$'\t'* ]] || fail "install root contains a control character"
[[ -d "$install_root" && ! -L "$install_root" ]] || fail "install root is missing or unsafe"
[[ "$(realpath -e -- "$install_root")" == "$install_root" ]] || fail "install root must be canonical"
[[ -f "$archive" && ! -L "$archive" ]] || fail "release archive is missing or unsafe"

# One inherited open-file description stays locked across every phase.
[[ "$lock_fd" =~ ^[1-9][0-9]{0,2}$ ]] \
    || fail "a transaction-owned deployment lock descriptor is required"
[[ "$lock_owner_pid" =~ ^[1-9][0-9]*$ ]] \
    || fail "a transaction lock owner PID is required"
[[ -e "/proc/$$/fd/$lock_fd" && -e "/proc/$lock_owner_pid/fd/$lock_fd" ]] \
    || fail "the inherited deployment lock descriptor is unavailable"
own_lock_target=$(realpath -e -- "/proc/$$/fd/$lock_fd") \
    || fail "cannot resolve the inherited deployment lock"
owner_lock_target=$(realpath -e -- "/proc/$lock_owner_pid/fd/$lock_fd") \
    || fail "cannot resolve the transaction owner's deployment lock"
expected_lock_target=$(realpath -e -- "$lock_file") \
    || fail "cannot resolve the fixed deployment lock"
[[ "$own_lock_target" == "$expected_lock_target" \
    && "$owner_lock_target" == "$expected_lock_target" ]] \
    || fail "the inherited deployment lock does not use the fixed host lock"
flock -n "$lock_fd" || fail "another controller deployment is already running"
kill -0 "$lock_owner_pid" 2>/dev/null \
    || fail "the transaction lock owner is no longer running"
printf '%s  %s\n' "$expected_sha256" "$archive" | sha256sum -c -

validate_live_server() {
    python3 - "$install_root" <<'PY'
# BEGIN trusted-live-server-validator
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def refuse(message: str) -> None:
    raise SystemExit(f"live Controller source is unsafe: {message}")


def require_root_tree(root: Path) -> None:
    try:
        root_stat = root.lstat()
    except OSError as exc:
        refuse(f"cannot inspect {root}: {exc}")
    if not stat.S_ISDIR(root_stat.st_mode):
        refuse(f"{root} is not a direct directory")
    if root_stat.st_uid != 0 or root_stat.st_mode & 0o022:
        refuse(f"{root} is not root-owned and non-writable by group/other")
    for directory, names, files in os.walk(root, followlinks=False):
        for name in [*names, *files]:
            candidate = Path(directory) / name
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                refuse(f"cannot inspect {candidate}: {exc}")
            if (
                not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
                or metadata.st_uid != 0
                or metadata.st_mode & 0o022
            ):
                refuse(f"untrusted entry in live tree: {candidate}")
    version_file = root / "__init__.py"
    try:
        version_stat = version_file.lstat()
    except OSError as exc:
        refuse(f"version file is missing: {exc}")
    if not stat.S_ISREG(version_stat.st_mode):
        refuse("version file is not regular")


install_root = Path(sys.argv[1])
live = install_root / "server"
if live.is_symlink() or not live.is_dir() or live.resolve(strict=True) != live:
    refuse("live path is not a canonical directory")
require_root_tree(live)
print(live)
# END trusted-live-server-validator
PY
}

prepared_state_action() {
    python3 - "$@" <<'PY'
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time


def refuse(message: str) -> None:
    raise SystemExit(f"prepared Controller state is unsafe: {message}")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def direct_directory(path: Path, *, mode: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        refuse(f"cannot inspect directory {path}: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        refuse(f"path is not a direct directory: {path}")
    if path.resolve(strict=True) != path:
        refuse(f"directory is not canonical: {path}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        refuse(f"directory is not root-owned and non-writable by group/other: {path}")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        refuse(f"directory mode is invalid: {path}")
    return metadata


def regular_file(path: Path, *, mode: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        refuse(f"cannot inspect file {path}: {exc}")
    if not stat.S_ISREG(metadata.st_mode):
        refuse(f"path is not a direct regular file: {path}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        refuse(f"file is not root-owned and non-writable by group/other: {path}")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        refuse(f"file mode is invalid: {path}")
    return metadata


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_identity(
    root: Path,
    *,
    root_mode: int | None = None,
    allow_venv_lib64: bool = False,
) -> dict[str, object]:
    root_stat = direct_directory(root, mode=root_mode)
    digest = hashlib.sha256()

    def record(kind: bytes, relative: str, mode: int, payload: bytes = b"") -> None:
        encoded = relative.encode("utf-8")
        digest.update(kind)
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    record(b"D", ".", stat.S_IMODE(root_stat.st_mode))

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            refuse(f"cannot scan prepared tree {directory}: {exc}")
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                refuse(f"cannot inspect prepared tree entry {path}: {exc}")
            if metadata.st_uid != 0:
                refuse(f"untrusted ownership in prepared tree: {path}")
            if not stat.S_ISLNK(metadata.st_mode) and metadata.st_mode & 0o022:
                refuse(f"untrusted mode in prepared tree: {path}")
            entry_mode = stat.S_IMODE(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                allowed = allow_venv_lib64 and relative == "lib64" and target == "lib"
                if not allowed:
                    refuse(f"unexpected symbolic link in prepared tree: {path}")
                resolved = (path.parent / target).resolve(strict=True)
                if root not in resolved.parents or not resolved.is_dir():
                    refuse(f"prepared virtual environment link escapes its root: {path}")
                record(b"L", relative, entry_mode, target.encode("utf-8"))
            elif stat.S_ISDIR(metadata.st_mode):
                record(b"D", relative, entry_mode)
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                content = hashlib.sha256()
                with path.open("rb") as source:
                    size = 0
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(block)
                        content.update(block)
                record(
                    b"F",
                    relative,
                    entry_mode,
                    size.to_bytes(8, "big") + content.digest(),
                )
            else:
                refuse(f"special file in prepared tree: {path}")

    visit(root)
    return {
        "path": str(root),
        "device": root_stat.st_dev,
        "inode": root_stat.st_ino,
        "mode": stat.S_IMODE(root_stat.st_mode),
        "tree_sha256": digest.hexdigest(),
    }


def shallow_tree_identity(root: Path, expected: dict[str, object]) -> None:
    metadata = direct_directory(root, mode=int(expected["mode"]))
    if metadata.st_dev != expected["device"] or metadata.st_ino != expected["inode"]:
        refuse(f"prepared tree identity changed: {root}")


def archive_identity(path: Path, expected_sha256: str) -> dict[str, object]:
    if path.resolve(strict=True) != path:
        refuse("archive path is not canonical")
    metadata = regular_file(path)
    actual_sha256 = file_sha256(path)
    if actual_sha256 != expected_sha256:
        refuse("archive SHA-256 changed")
    return {
        "path": str(path),
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "sha256": actual_sha256,
    }


def live_venv_identity(install_root: Path) -> dict[str, object]:
    live = install_root / ".venv"
    try:
        live_stat = live.lstat()
    except OSError as exc:
        refuse(f"cannot inspect live virtual environment: {exc}")
    if stat.S_ISLNK(live_stat.st_mode):
        if live_stat.st_uid != 0:
            refuse("live virtual environment link is not root-owned")
        target = live.resolve(strict=True)
        managed = install_root / "venvs"
        if managed not in target.parents or not target.name.startswith("controller-"):
            refuse("live virtual environment target is outside the managed root")
        kind = "symlink"
    elif stat.S_ISDIR(live_stat.st_mode):
        if live.resolve(strict=True) != live:
            refuse("live virtual environment directory is not canonical")
        target = live
        kind = "directory"
    else:
        refuse("live virtual environment is missing or unsafe")
    target_stat = direct_directory(target)
    return {
        "kind": kind,
        "target": str(target),
        "device": target_stat.st_dev,
        "inode": target_stat.st_ino,
    }


def strict_state_dir(path: Path, install_root: Path) -> None:
    releases = install_root / "releases"
    direct_directory(releases)
    if path.parent != releases or re.fullmatch(
        r"\.controller-prepare-(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
        r"(?:0|[1-9][0-9]*)-[0-9TZ-]+-[0-9a-f]{24}",
        path.name,
    ) is None:
        refuse("prepared state path is outside the fixed release root")
    direct_directory(path, mode=0o700)


def canonical_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_state(
    state_dir: Path,
    archive: Path,
    expected_sha256: str,
    version: str,
    install_root: Path,
    service: str,
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    strict_state_dir(state_dir, install_root)
    token_path = state_dir / "token"
    state_path = state_dir / "state.json"
    regular_file(token_path, mode=0o600)
    regular_file(state_path, mode=0o600)
    token_text = token_path.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{64}", token_text) is None:
        refuse("prepared token is invalid")
    token = bytes.fromhex(token_text)
    document = json.loads(state_path.read_text(encoding="utf-8"))
    if set(document) != {"payload", "hmac_sha256"} or not isinstance(
        document["payload"], dict
    ):
        refuse("prepared state document fields are invalid")
    payload = document["payload"]
    actual_hmac = hmac.new(token, canonical_payload(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(actual_hmac, document["hmac_sha256"]):
        refuse("prepared state HMAC is invalid")
    expected_fields = {
        "schema_version", "created_at", "expires_at", "version", "service",
        "install_root", "archive", "stage", "venv", "live_server", "live_venv",
    }
    if set(payload) != expected_fields or payload["schema_version"] != 1:
        refuse("prepared state payload fields are invalid")
    if (
        payload["version"] != version
        or payload["service"] != service
        or payload["install_root"] != str(install_root)
    ):
        refuse("prepared state identity does not match this transaction")
    current_archive = archive_identity(archive, expected_sha256)
    if current_archive != payload["archive"]:
        refuse("prepared archive identity changed")
    return payload, token, document


action = sys.argv[1]
state_dir = Path(sys.argv[2])
archive = Path(sys.argv[3])
expected_sha256 = sys.argv[4]
version = sys.argv[5]
install_root = Path(sys.argv[6])
service = sys.argv[7]

if action == "create":
    stage = Path(sys.argv[8])
    venv = Path(sys.argv[9])
    ttl = int(sys.argv[10])
    strict_state_dir(state_dir, install_root)
    now = int(time.time())
    payload = {
        "schema_version": 1,
        "created_at": now,
        "expires_at": now + ttl,
        "version": version,
        "service": service,
        "install_root": str(install_root),
        "archive": archive_identity(archive, expected_sha256),
        "stage": tree_identity(stage, root_mode=0o700),
        "venv": tree_identity(venv, root_mode=0o755, allow_venv_lib64=True),
        "live_server": tree_identity(install_root / "server"),
        "live_venv": live_venv_identity(install_root),
    }
    token_text = secrets.token_hex(32)
    token = bytes.fromhex(token_text)
    document = {
        "payload": payload,
        "hmac_sha256": hmac.new(
            token, canonical_payload(payload), hashlib.sha256
        ).hexdigest(),
    }
    atomic_write(state_dir / "token", (token_text + "\n").encode("ascii"))
    atomic_write(
        state_dir / "state.json",
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )
    print(state_dir)
elif action in {"validate", "abort", "mark-committed", "finalize"}:
    payload, token, document = load_state(
        state_dir, archive, expected_sha256, version, install_root, service
    )
    incomplete = state_dir / "rollback-incomplete"
    if action in {"validate", "abort"} and (incomplete.exists() or incomplete.is_symlink()):
        refuse("runtime rollback is incomplete; preserve the recovery state")
    stage = Path(payload["stage"]["path"])
    venv = Path(payload["venv"]["path"])
    if action == "validate":
        now = int(time.time())
        if not payload["created_at"] <= now <= payload["expires_at"]:
            refuse("prepared state is stale")
        if tree_identity(stage, root_mode=0o700) != payload["stage"]:
            refuse("prepared server tree changed")
        if tree_identity(
            venv, root_mode=0o755, allow_venv_lib64=True
        ) != payload["venv"]:
            refuse("prepared virtual environment tree changed")
        if tree_identity(install_root / "server") != payload["live_server"]:
            refuse("live Controller source changed after prepare")
        if live_venv_identity(install_root) != payload["live_venv"]:
            refuse("live virtual environment changed after prepare")
        print(stage)
        print(venv)
        print(payload["live_venv"]["kind"])
        print(payload["live_venv"]["target"])
    elif action == "abort":
        shallow_tree_identity(stage, payload["stage"])
        shallow_tree_identity(venv, payload["venv"])
        print(stage)
        print(venv)
    elif action == "mark-committed":
        active_venv = (install_root / ".venv").resolve(strict=True)
        if active_venv != venv:
            refuse("committed virtual environment target is invalid")
        receipt_payload = {
            "schema_version": 1,
            "state_hmac_sha256": document["hmac_sha256"],
            "committed_at": int(time.time()),
            "live_server": tree_identity(install_root / "server"),
            "active_venv": str(active_venv),
        }
        receipt = {
            "payload": receipt_payload,
            "hmac_sha256": hmac.new(
                token, canonical_payload(receipt_payload), hashlib.sha256
            ).hexdigest(),
        }
        atomic_write(
            state_dir / "committed.json",
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
        print(state_dir)
    else:
        receipt_path = state_dir / "committed.json"
        regular_file(receipt_path, mode=0o600)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if set(receipt) != {"payload", "hmac_sha256"} or not isinstance(
            receipt["payload"], dict
        ):
            refuse("commit receipt fields are invalid")
        receipt_payload = receipt["payload"]
        expected_receipt_fields = {
            "schema_version", "state_hmac_sha256", "committed_at",
            "live_server", "active_venv",
        }
        if set(receipt_payload) != expected_receipt_fields:
            refuse("commit receipt payload fields are invalid")
        actual_hmac = hmac.new(
            token, canonical_payload(receipt_payload), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(actual_hmac, receipt["hmac_sha256"]):
            refuse("commit receipt HMAC is invalid")
        if (
            receipt_payload["schema_version"] != 1
            or receipt_payload["state_hmac_sha256"] != document["hmac_sha256"]
            or receipt_payload["active_venv"] != str(venv)
            or (install_root / ".venv").resolve(strict=True) != venv
            or tree_identity(install_root / "server") != receipt_payload["live_server"]
        ):
            refuse("committed live identity changed")
        print(state_dir)
else:
    refuse(f"unsupported prepared-state action: {action}")
PY
}

release_root="$install_root/releases"
install -d -m 700 -- "$release_root"

if [[ "$mode" == "abort" ]]; then
    abort_output=$(prepared_state_action abort "$prepared_state" "$archive" \
        "$expected_sha256" "$version" "$install_root" "$service") \
        || fail "prepared Controller abort validation failed"
    mapfile -t abort_paths <<<"$abort_output"
    [[ "${#abort_paths[@]}" -eq 2 ]] || fail "prepared abort result is invalid"
    abort_stage=${abort_paths[0]}
    abort_venv=${abort_paths[1]}
    case "$abort_stage" in "$install_root"/.deploy-*) ;; *) fail "unsafe prepared stage path" ;; esac
    case "$abort_venv" in "$install_root"/venvs/controller-*) ;; *) fail "unsafe prepared virtual environment path" ;; esac
    case "$prepared_state" in "$release_root"/.controller-prepare-*) ;; *) fail "unsafe prepared state path" ;; esac
    rm -rf --one-file-system -- "$abort_stage"
    rm -rf --one-file-system -- "$abort_venv"
    rm -rf --one-file-system -- "$prepared_state"
    echo "ABORTED_PREPARED_STATE=$prepared_state"
    exit 0
fi

if [[ "$mode" == "finalize" ]]; then
    finalized=$(prepared_state_action finalize "$prepared_state" "$archive" \
        "$expected_sha256" "$version" "$install_root" "$service") \
        || fail "committed Controller receipt validation failed"
    [[ "$finalized" == "$prepared_state" ]] || fail "commit finalize result is invalid"
    case "$prepared_state" in "$release_root"/.controller-prepare-*) ;; *) fail "unsafe prepared state path" ;; esac
    rm -rf --one-file-system -- "$prepared_state"
    echo "FINALIZED_PREPARED_STATE=$prepared_state"
    exit 0
fi

if [[ "$mode" == "prepare" ]]; then
initial_live_server=$(validate_live_server) \
    || fail "live Controller source validation failed"
[[ "$initial_live_server" == "$install_root/server" ]] \
    || fail "live Controller source path is invalid"

nonce=$(python3 -c 'import secrets; print(secrets.token_hex(12))')
stamp=$(date -u +%Y%m%dT%H%M%SZ)-$$-$nonce
stage="$install_root/.deploy-$version-$stamp"
venv_root="$install_root/venvs"
new_venv="$venv_root/controller-$version-$stamp"
state_dir="$release_root/.controller-prepare-$version-$stamp"
install -d -m 755 -- "$venv_root"
for path in "$stage" "$new_venv" "$state_dir"; do
    [[ ! -e "$path" && ! -L "$path" ]] || fail "deployment path already exists: $path"
done

prepared_published=0
cleanup_preflight() {
    local exit_code=${1:-1}
    trap - ERR HUP INT TERM EXIT
    set +e
    if [[ "$prepared_published" -eq 0 ]]; then
        [[ -d "$stage" && ! -L "$stage" ]] && rm -rf --one-file-system -- "$stage"
        [[ -d "$new_venv" && ! -L "$new_venv" ]] && rm -rf --one-file-system -- "$new_venv"
        [[ -d "$state_dir" && ! -L "$state_dir" ]] && rm -rf --one-file-system -- "$state_dir"
    fi
    exit "$exit_code"
}

trap 'cleanup_preflight $?' ERR
trap 'cleanup_preflight 129' HUP
trap 'cleanup_preflight 130' INT
trap 'cleanup_preflight 143' TERM
trap 'cleanup_preflight $?' EXIT

mkdir -m 700 -- "$stage"
# Validate the manifest and every archive member before extracting anything.
python3 - "$archive" "$stage" "$version" "$install_root" <<'PY'
from __future__ import annotations

import ctypes
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import re
import sys
import tarfile
import zipfile

archive_path = Path(sys.argv[1])
stage = Path(sys.argv[2])
expected_version = sys.argv[3]
install_root = Path(sys.argv[4])
manifest_name = "controller-release.json"
runtime_root = "deploy/controller-runtime"
wheelhouse = f"{runtime_root}/wheelhouse/linux-x86_64-py310"
allowed_files = {
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
}
allowed_directories: set[str] = set()
for name in allowed_files:
    parent = PurePosixPath(name).parent
    while str(parent) != ".":
        allowed_directories.add(parent.as_posix())
        parent = parent.parent

def canonical(name: str) -> str:
    value = name.rstrip("/")
    path = PurePosixPath(value)
    if (not value or "\\" in value or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != value):
        raise RuntimeError(f"unsafe archive path: {name!r}")
    return value

with tarfile.open(archive_path, mode="r:gz") as handle:
    members = handle.getmembers()
    if not 1 <= len(members) <= len(allowed_files) + len(allowed_directories) + 1:
        raise RuntimeError("controller archive entry count is invalid")
    by_name: dict[str, tarfile.TarInfo] = {}
    folded: set[str] = set()
    total_size = 0
    for member in members:
        name = canonical(member.name)
        if name in by_name or name.casefold() in folded:
            raise RuntimeError("controller archive contains duplicate paths")
        if not (member.isdir() or member.isreg()):
            raise RuntimeError("controller archive contains a link or special file")
        if member.size < 0 or member.size > 16 * 1024 * 1024:
            raise RuntimeError("controller archive member is too large")
        total_size += member.size
        if total_size > 64 * 1024 * 1024:
            raise RuntimeError("controller archive expands beyond the release limit")
        by_name[name] = member
        folded.add(name.casefold())

    manifest_member = by_name.get(manifest_name)
    if manifest_member is None or not manifest_member.isreg() or manifest_member.size > 1024 * 1024:
        raise RuntimeError("controller release manifest is missing or invalid")
    manifest_stream = handle.extractfile(manifest_member)
    if manifest_stream is None:
        raise RuntimeError("controller release manifest cannot be read")
    manifest = json.loads(manifest_stream.read().decode("utf-8"))
    expected_manifest_fields = {"schema_version", "version", "files"}
    if set(manifest) != expected_manifest_fields:
        raise RuntimeError("controller release manifest fields are invalid")
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["version"] != expected_version):
        raise RuntimeError("controller release manifest does not match this host")
    declared = manifest["files"]
    if not isinstance(declared, dict) or set(declared) != allowed_files:
        raise RuntimeError("controller release file allowlist does not match")
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in declared.values()):
        raise RuntimeError("controller release manifest contains an invalid digest")
    regular_files = {name for name, member in by_name.items() if member.isreg()}
    directories = {name for name, member in by_name.items() if member.isdir()}
    if regular_files != allowed_files | {manifest_name}:
        raise RuntimeError("controller archive contains undeclared or missing files")
    if not directories.issubset(allowed_directories):
        raise RuntimeError("controller archive contains an unexpected directory")

    for name in sorted(allowed_files):
        member = by_name[name]
        source = handle.extractfile(member)
        if source is None:
            raise RuntimeError(f"controller archive file cannot be read: {name}")
        target = stage.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        digest = hashlib.sha256()
        written = 0
        with target.open("xb") as output:
            while block := source.read(1024 * 1024):
                written += len(block)
                if written > member.size:
                    raise RuntimeError(f"controller archive member size changed: {name}")
                digest.update(block)
                output.write(block)
        if written != member.size or digest.hexdigest() != declared[name]:
            raise RuntimeError(f"controller release hash mismatch: {name}")
        target.chmod(0o644)

version_source = (stage / "server" / "__init__.py").read_text(encoding="utf-8")
version_pattern = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
match = re.search(
    r'^__version__\s*=\s*["\x27](' + version_pattern + r')["\x27]\s*$',
    version_source,
    re.MULTILINE,
)
if match is None or match.group(1) != expected_version:
    raise RuntimeError("controller source version does not match the requested release")

expected_wheels = {name for name in allowed_files if name.startswith(f"{wheelhouse}/")}
runtime_target = json.loads(
    (stage / runtime_root / "runtime-target.json").read_text(encoding="utf-8")
)
expected_target = {
    "schema_version": 1,
    "controller_version": expected_version,
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
        "lock": f"{runtime_root}/requirements-controller-linux-py310.lock",
        "wheelhouse": wheelhouse,
        "wheel_count": len(expected_wheels),
        "only_binary": True,
        "require_hashes": True,
    },
}
if (runtime_target != expected_target
        or type(runtime_target["schema_version"]) is not int
        or type(runtime_target["dependencies"]["wheel_count"]) is not int
        or runtime_target["dependencies"]["only_binary"] is not True
        or runtime_target["dependencies"]["require_hashes"] is not True):
    raise RuntimeError("controller runtime target is invalid")
if platform.system() != "Linux" or platform.machine() != "x86_64":
    raise RuntimeError("controller runtime requires Linux x86_64")
if platform.python_implementation() != "CPython" or sys.version_info[:2] != (3, 10):
    raise RuntimeError("controller runtime requires CPython 3.10")
try:
    glibc_version = ctypes.CDLL(None).gnu_get_libc_version
    glibc_version.restype = ctypes.c_char_p
    raw_glibc = glibc_version()
    glibc_match = re.fullmatch(rb"([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", raw_glibc)
except (AttributeError, OSError, TypeError) as exc:
    raise RuntimeError("controller runtime requires glibc >= 2.35") from exc
if glibc_match is None or tuple(map(int, glibc_match.groups())) < (2, 35):
    raise RuntimeError("controller runtime requires glibc >= 2.35")

lock_re = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?==([A-Za-z0-9_.+!-]+) "
    r"--hash=sha256:([0-9a-f]{64})$"
)
locked: dict[str, tuple[str, str]] = {}
lock_path = stage / runtime_target["dependencies"]["lock"]
for raw in lock_path.read_text(encoding="utf-8").splitlines():
    line = raw.split("#", 1)[0].strip()
    if not line:
        continue
    lock_match = lock_re.fullmatch(line)
    if lock_match is None:
        raise RuntimeError("controller production lock is not exact and singly hashed")
    name, locked_version, digest = lock_match.groups()
    name = re.sub(r"[-_.]+", "-", name).lower()
    if name in locked:
        raise RuntimeError("controller production lock contains a duplicate")
    locked[name] = (locked_version, digest)
wheel_paths = {
    path.relative_to(stage).as_posix(): path
    for path in (stage / wheelhouse).glob("*.whl")
    if path.is_file() and not path.is_symlink()
}
if set(wheel_paths) != expected_wheels:
    raise RuntimeError("controller wheelhouse file set is invalid")
wheels: dict[str, tuple[str, str]] = {}
for path in wheel_paths.values():
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
    name = re.sub(r"[-_.]+", "-", metadata["Name"]).lower()
    if name in wheels:
        raise RuntimeError(f"duplicate controller wheel distribution: {name}")
    wheels[name] = (
        metadata["Version"],
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
if wheels != locked:
    raise RuntimeError(
        "controller wheelhouse identities and hashes do not match the production lock"
    )
PY

# Build a complete new environment without touching the live one.  Release
# requirements and every transitive dependency are exact, singly hashed, and
# shipped in the archive.  pip cannot consult an index or accept a source tree.
python3 - "$stage/server/requirements.txt" <<'PY'
from pathlib import Path
import re
import sys

lines = []
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw.split("#", 1)[0].strip()
    if line:
        lines.append(line)
pattern = re.compile(r"^[A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_.,-]+\])?==[A-Za-z0-9_.+!-]+$")
if not lines or any(pattern.fullmatch(line) is None for line in lines):
    raise RuntimeError("controller requirements must contain only exact name==version pins")
PY

runtime_root="$stage/deploy/controller-runtime"
production_lock="$runtime_root/requirements-controller-linux-py310.lock"
wheelhouse="$runtime_root/wheelhouse/linux-x86_64-py310"
python3 -m venv --copies "$new_venv"
PIP_CONFIG_FILE=/dev/null PIP_NO_INDEX=1 PIP_ONLY_BINARY=:all: \
    "$new_venv/bin/python" -m pip install \
    --disable-pip-version-check --no-input \
    --no-index --only-binary=:all: --require-hashes \
    --find-links "$wheelhouse" --requirement "$production_lock"
"$new_venv/bin/python" -m pip check
PYTHONPYCACHEPREFIX="$stage/.compile-cache" \
    "$new_venv/bin/python" -m compileall -q "$stage/server"
rm -rf --one-file-system -- "$stage/.compile-cache"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$stage" \
    "$new_venv/bin/python" - "$version" <<'PY'
import sys
import fastapi
import multipart
import uvicorn
from server import __version__ as imported_version

if imported_version != sys.argv[1]:
    raise SystemExit("staged controller version import does not match")
PY

# Import the complete staged V1-only application before entering the
# service-changing transaction. Use a fresh, isolated data root and a
# scrubbed environment: preflight must never open or migrate live state.
preflight_data="$stage/.preflight-data"
install -d -m 700 -- "$preflight_data"
env -i \
    HOME="$preflight_data" TMPDIR="$preflight_data" \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$stage" \
    WEB_CONCURRENCY=1 UVICORN_WORKERS=1 GUNICORN_WORKERS=1 \
    RIGPILOT_DATA_DIR="$preflight_data" \
    RIGPILOT_DATABASE="$preflight_data/rigpilot.db" \
    RIGPILOT_SECRET_KEY='RigPilot-controller-release-preflight-only-2026!' \
    RIGPILOT_ADMIN_USERNAME=preflight \
    RIGPILOT_ADMIN_PASSWORD='RigPilot-preflight-password-not-for-login' \
    "$new_venv/bin/python" - "$version" <<'PY'
import sys

from server import __version__
from server.main import app

if __version__ != sys.argv[1] or app.version != sys.argv[1]:
    raise SystemExit("staged Controller application version does not match")
paths = {getattr(route, "path", None) for route in app.routes}
required_paths = {
    "/api/health",
    "/api/agents",
    "/api/flight-sheets",
    "/api/agents/{agent_id}/flight-sheets/{sheet_id}/apply",
}
forbidden_paths = {
    "/api/flight-sheets/v2",
    "/api/miner-adapters/v2",
}
if not required_paths.issubset(paths):
    raise SystemExit("staged Controller V1 routes are incomplete")
if forbidden_paths.intersection(paths):
    raise SystemExit("staged Controller unexpectedly exposes Flight V2 routes")
PY
rm -rf --one-file-system -- "$preflight_data"
rm -rf --one-file-system -- "$stage/deploy"
chown -R root:root "$stage/server" "$new_venv"
find "$stage/server" -type d -exec chmod 755 {} +
find "$stage/server" -type f -exec chmod 644 {} +
# The data-snapshot wrapper deliberately uses umask 077.  Normalize the
# isolated environment explicitly so the unprivileged controller service can
# traverse and execute it without granting group/other write access.
chmod -R a+rX,u+w,go-w "$new_venv"
install -d -m 700 -- "$state_dir"
created_state=$(prepared_state_action create "$state_dir" "$archive" \
    "$expected_sha256" "$version" "$install_root" "$service" \
    "$stage" "$new_venv" "$prepare_ttl_seconds") \
    || fail "could not publish prepared Controller state"
[[ "$created_state" == "$state_dir" ]] || fail "prepared state result is invalid"
result_file=${RIGPILOT_PREPARE_RESULT_FILE:-}
[[ -n "$result_file" && -f "$result_file" && ! -L "$result_file" ]] \
    || fail "a root-owned prepare result file is required"
python3 - "$result_file" "$state_dir" <<'PY'
import os
from pathlib import Path
import stat
import sys

path = Path(sys.argv[1])
metadata = path.lstat()
if (
    not stat.S_ISREG(metadata.st_mode)
    or metadata.st_uid != 0
    or stat.S_IMODE(metadata.st_mode) != 0o600
):
    raise SystemExit("prepare result file is unsafe")
with path.open("w", encoding="utf-8", newline="\n") as output:
    output.write(sys.argv[2] + "\n")
    output.flush()
    os.fsync(output.fileno())
PY
prepared_published=1
trap - ERR HUP INT TERM EXIT
echo "PREPARED_STATE=$state_dir"
exit 0
fi

# The other accepted phases have returned before activation.
state_dir=$prepared_state
validated_state=$(prepared_state_action validate "$state_dir" "$archive" \
    "$expected_sha256" "$version" "$install_root" "$service") \
    || fail "prepared Controller commit validation failed"
mapfile -t prepared_values <<<"$validated_state"
[[ "${#prepared_values[@]}" -eq 4 ]] || fail "prepared commit result is invalid"
stage=${prepared_values[0]}
new_venv=${prepared_values[1]}
prepared_old_venv_kind=${prepared_values[2]}
prepared_old_venv_target=${prepared_values[3]}
venv_root="$install_root/venvs"
state_name=${state_dir##*/}
stamp=${state_name#".controller-prepare-$version-"}
[[ -n "$stamp" && "$stamp" != "$state_name" ]] || fail "prepared state stamp is invalid"
backup="$release_root/server-before-$version-$stamp"
failed="$release_root/server-failed-$version-$stamp"
backup_venv="$release_root/venv-before-$version-$stamp"
failed_venv="$release_root/venv-failed-$version-$stamp"
case "$stage" in "$install_root"/.deploy-*) ;; *) fail "unsafe prepared stage path" ;; esac
case "$new_venv" in "$venv_root"/controller-*) ;; *) fail "unsafe prepared virtual environment path" ;; esac
for path in "$backup" "$failed" "$backup_venv" "$failed_venv"; do
    [[ ! -e "$path" && ! -L "$path" ]] || fail "commit path already exists: $path"
done
systemctl is-active --quiet "$service" \
    && fail "Controller service must be stopped by the snapshot transaction before commit"

initial_live_server="$install_root/server"
# Revalidate the current live pair after the offline snapshot and before any
# live rename. The prepared HMAC also binds its tree and inode identities.
current_live_server=$(validate_live_server) \
    || fail "live Controller source validation failed before activation"
[[ "$current_live_server" == "$initial_live_server" ]] \
    || fail "live Controller source changed during deployment preflight"
old_venv_kind=""
old_venv_target=""
if [[ -L "$install_root/.venv" ]]; then
    old_venv_target=$(realpath -e -- "$install_root/.venv") || fail "live virtual environment link is broken"
    case "$old_venv_target" in "$venv_root"/controller-*) ;; *) fail "live virtual environment link is outside the managed root" ;; esac
    [[ -d "$old_venv_target" && ! -L "$old_venv_target" ]] || fail "live virtual environment target is unsafe"
    old_venv_kind="symlink"
elif [[ -d "$install_root/.venv" && ! -L "$install_root/.venv" ]]; then
    [[ "$(realpath -e -- "$install_root/.venv")" == "$install_root/.venv" ]] || fail "live virtual environment is unsafe"
    old_venv_kind="directory"
else
    fail "live virtual environment is missing or unsafe"
fi
[[ "$old_venv_kind" == "$prepared_old_venv_kind" ]] \
    || fail "live virtual environment identity changed after prepare"
if [[ "$old_venv_kind" == "symlink" \
        && "$old_venv_target" != "$prepared_old_venv_target" ]]; then
    fail "live virtual environment target changed after prepare"
fi
trap - ERR HUP INT TERM EXIT
live_change_started=0

rollback() {
    local exit_code=${1:-1}
    trap - ERR HUP INT TERM EXIT
    set +e
    rollback_refused() {
        # Preserve every remaining runtime/data recovery asset. The wrapper
        # also checks this marker when a signal masks the child's exit status.
        if [[ -d "$state_dir" && ! -L "$state_dir" ]]; then
            (umask 077; set -C; printf '%s\n' 'runtime recovery failed' \
                > "$state_dir/rollback-incomplete") 2>/dev/null || true
        fi
        printf 'Controller runtime rollback requires operator attention.\n' >&2
        exit 70
    }
    if [[ "$live_change_started" -eq 1 ]]; then
        if ! systemctl stop "$service" 2>/dev/null \
                || systemctl is-active --quiet "$service"; then
            rollback_refused
        fi
        # Recover from durable filesystem state: a signal can arrive
        # immediately after rename(2) succeeds.
        if [[ -e "$backup" || -L "$backup" ]]; then
            [[ -d "$backup" && ! -L "$backup" ]] || rollback_refused
            if [[ -d "$install_root/server" && ! -L "$install_root/server" ]]; then
                [[ ! -e "$failed" && ! -L "$failed" ]] || rollback_refused
                mv -T -- "$install_root/server" "$failed" || rollback_refused
            elif [[ -e "$install_root/server" || -L "$install_root/server" ]]; then
                rollback_refused
            fi
        fi
        if [[ -L "$install_root/.venv" ]]; then
            active_venv=$(realpath -e -- "$install_root/.venv" 2>/dev/null || true)
            if [[ "$active_venv" == "$new_venv" ]]; then
                rm -f -- "$install_root/.venv" || rollback_refused
            fi
        fi
        if [[ ( -d "$backup_venv" && ! -L "$backup_venv" ) && -d "$new_venv" && ! -L "$new_venv" ]]; then
            [[ ! -e "$failed_venv" && ! -L "$failed_venv" ]] || rollback_refused
            mv -T -- "$new_venv" "$failed_venv" || rollback_refused
        fi
        if [[ -d "$backup_venv" && ! -L "$backup_venv" ]]; then
            if [[ "$old_venv_kind" == "symlink" ]]; then
                [[ ! -e "$old_venv_target" && ! -L "$old_venv_target" ]] || rollback_refused
                mv -T -- "$backup_venv" "$old_venv_target" || rollback_refused
                ln -s -- "$old_venv_target" "$install_root/.venv" || rollback_refused
            elif [[ ! -e "$install_root/.venv" && ! -L "$install_root/.venv" ]]; then
                mv -T -- "$backup_venv" "$install_root/.venv" || rollback_refused
            else
                rollback_refused
            fi
        elif [[ "$old_venv_kind" == "symlink" \
                && ! -e "$install_root/.venv" && ! -L "$install_root/.venv" \
                && -d "$old_venv_target" && ! -L "$old_venv_target" ]]; then
            # The old link was removed but its target had not yet been moved.
            ln -s -- "$old_venv_target" "$install_root/.venv" || rollback_refused
        fi
        if [[ ! -e "$install_root/server" && ! -L "$install_root/server" \
                && -d "$backup" && ! -L "$backup" ]]; then
                mv -T -- "$backup" "$install_root/server" || rollback_refused
        fi
    fi
    [[ -d "$stage" && ! -L "$stage" ]] && rm -rf --one-file-system -- "$stage"
    [[ -d "$new_venv" && ! -L "$new_venv" ]] && rm -rf --one-file-system -- "$new_venv"
    if [[ "$live_change_started" -eq 1 \
            && -d "$state_dir" && ! -L "$state_dir" ]]; then
        rm -rf --one-file-system -- "$state_dir"
    fi
    exit "$exit_code"
}

trap 'rollback $?' ERR
trap 'rollback 129' HUP
trap 'rollback 130' INT
trap 'rollback 143' TERM

live_change_started=1
mv -T -- "$install_root/server" "$backup"
if [[ "$old_venv_kind" == "symlink" ]]; then
    rm -f -- "$install_root/.venv"
    mv -- "$old_venv_target" "$backup_venv"
else
    mv -- "$install_root/.venv" "$backup_venv"
fi
ln -s -- "$new_venv" "$install_root/.venv"
mv -- "$stage/server" "$install_root/server"
rmdir -- "$stage"

if ! systemctl start "$service"; then
    rollback 1
fi

healthy=0
for _attempt in $(seq 1 "$health_wait_seconds"); do
    if health_json=$(curl -fsS --max-time 2 "http://127.0.0.1:$health_port/api/health" 2>/dev/null) \
        && printf '%s' "$health_json" | grep -Fq '"status":"ok"' \
        && printf '%s' "$health_json" | grep -Fq "\"version\":\"$version\""; then
        healthy=1
        break
    fi
    sleep 1
done

if [[ "$healthy" -ne 1 ]]; then
    journalctl -u "$service" -n 80 --no-pager || true
    rollback 1
fi
if ! systemctl is-active --quiet "$service"; then
    rollback 1
fi
committed_state=$(prepared_state_action mark-committed "$state_dir" "$archive" \
    "$expected_sha256" "$version" "$install_root" "$service") \
    || rollback 1
[[ "$committed_state" == "$state_dir" ]] || rollback 1

trap - ERR HUP INT TERM EXIT
echo "DEPLOYED=$version"
echo "SERVER_BACKUP=$backup"
echo "VENV_BACKUP=$backup_venv"
echo "COMMIT_RECEIPT=$state_dir/committed.json"
printf '%s\n' "$health_json"
