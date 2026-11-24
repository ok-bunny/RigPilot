#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_ROOT}/../.." && pwd -P)"
VERSION="${1:-}"
OUTPUT_DIR="${2:-${PROJECT_ROOT}/outputs}"
WORK_PARENT=""
WORK_ROOT=""
ARCHIVE_STAGE=""
ARCHIVE_HASH_STAGE=""
ONLINE_STAGE=""
ONLINE_HASH_STAGE=""
umask 022

cleanup_release_stages() {
    set +e
    for stage in "${ARCHIVE_STAGE}" "${ARCHIVE_HASH_STAGE}" \
        "${ONLINE_STAGE}" "${ONLINE_HASH_STAGE}"; do
        [[ -n "${stage}" ]] && rm -f -- "${stage}"
    done
    if [[ -n "${WORK_ROOT}" && -n "${WORK_PARENT}" && -d "${WORK_ROOT}" && \
          ! -L "${WORK_ROOT}" ]]; then
        case "${WORK_ROOT}" in
            "${WORK_PARENT}"/*) rm -rf -- "${WORK_ROOT}" ;;
        esac
    fi
}
trap cleanup_release_stages EXIT

fail() {
    printf 'Ubuntu Agent build failed: %s\n' "$*" >&2
    exit 1
}

normalize_linux_text_tree() {
    local root="$1"
    PYTHONPATH="${PROJECT_ROOT}" "${PYTHON:-python3}" - "${root}" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
text_names = {"SHA256SUMS", "VERSION"}
text_suffixes = {".conf", ".json", ".md", ".py", ".service", ".sh", ".timer", ".txt"}
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    if path.name not in text_names and path.suffix.casefold() not in text_suffixes:
        continue
    payload = path.read_bytes()
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if normalized != payload:
        path.write_bytes(normalized)
PY
}

verify_linux_text_tree_lf() {
    local root="$1"
    PYTHONPATH="${PROJECT_ROOT}" "${PYTHON:-python3}" - "${root}" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
text_names = {"SHA256SUMS", "VERSION"}
text_suffixes = {".conf", ".json", ".md", ".py", ".service", ".sh", ".timer", ".txt"}
invalid = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path.is_symlink():
        continue
    if path.name not in text_names and path.suffix.casefold() not in text_suffixes:
        continue
    if b"\r" in path.read_bytes():
        invalid.append(path.relative_to(root).as_posix())
if invalid:
    raise SystemExit("Linux text files must use LF only: " + ", ".join(invalid))
PY
}

verify_linux_archive_text_lf() {
    local archive="$1"
    PYTHONPATH="${PROJECT_ROOT}" "${PYTHON:-python3}" - "${archive}" <<'PY'
import pathlib
import sys
import tarfile

archive_path = pathlib.Path(sys.argv[1])
text_names = {"SHA256SUMS", "VERSION"}
text_suffixes = {".conf", ".json", ".md", ".py", ".service", ".sh", ".timer", ".txt"}
invalid = []
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if not member.isreg():
            continue
        if path.name not in text_names and path.suffix.casefold() not in text_suffixes:
            continue
        source = archive.extractfile(member)
        if source is None or b"\r" in source.read():
            invalid.append(member.name)
if invalid:
    raise SystemExit("Linux archive contains non-LF text files: " + ", ".join(invalid))
PY
}

check_build_host() {
    # Keep os-release's VERSION field from overwriting the requested Agent VERSION.
    local ID="" VERSION_ID="" VERSION=""
    local glibc_report glibc_version glibc_major glibc_minor

    [[ "$(uname -s)" == "Linux" ]] || fail "build this package on Linux"
    case "$(uname -m)" in x86_64|amd64) ;; *) fail "an x86_64 build host is required" ;; esac
    [[ -r /etc/os-release ]] || fail "/etc/os-release is required"
    # Ubuntu owns this root-controlled file; use it only to select the release baseline.
    # shellcheck disable=SC1091
    source /etc/os-release
    [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "22.04" ]] || \
        fail "release packages must be built on Ubuntu 22.04, not ${ID:-unknown} ${VERSION_ID:-unknown}"
    command -v getconf >/dev/null 2>&1 || fail "getconf is required"
    glibc_report=$(getconf GNU_LIBC_VERSION 2>/dev/null) || fail "GNU glibc is required"
    [[ "${glibc_report}" =~ ^glibc[[:space:]]+([0-9]+)\.([0-9]+) ]] || \
        fail "cannot determine the glibc version"
    glibc_version=${BASH_REMATCH[0]#glibc }
    glibc_major=${BASH_REMATCH[1]}
    glibc_minor=${BASH_REMATCH[2]}
    (( glibc_major < 2 || (glibc_major == 2 && glibc_minor <= 35) )) || \
        fail "glibc ${glibc_version} is newer than the supported 2.35 build baseline"
    printf 'Build baseline: Ubuntu %s, glibc %s, %s\n' \
        "${VERSION_ID}" "${glibc_version}" "$(uname -m)"
}

check_build_host
if [[ "${1:-}" == "--check-build-host" ]]; then
    exit 0
fi
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v tar >/dev/null 2>&1 || fail "GNU tar is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"
for build_command in find sort xargs awk tr gzip install mktemp; do
    command -v "${build_command}" >/dev/null 2>&1 || fail "${build_command} is required"
done

SOURCE_VERSION="$(PYTHONPATH="${PROJECT_ROOT}" python3 -c 'from agent.version import __version__; print(__version__)')"
VERSION="${VERSION:-${SOURCE_VERSION}}"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "version must be X.Y.Z"
[[ "${VERSION}" == "${SOURCE_VERSION}" ]] || \
    fail "requested version ${VERSION} does not match Agent source ${SOURCE_VERSION}"

WORK_PARENT="${RIGPILOT_LINUX_WORK_PARENT:-${TMPDIR:-/tmp}/rigpilot-agent-build}"
if [[ -e "${WORK_PARENT}" && -L "${WORK_PARENT}" ]]; then
    fail "Linux build work parent must not be a symlink"
fi
mkdir -p -- "${WORK_PARENT}"
WORK_PARENT="$(cd -- "${WORK_PARENT}" && pwd -P)"
WORK_ROOT="$(mktemp -d "${WORK_PARENT}/ubuntu-portable.XXXXXXXX")"
case "${WORK_ROOT}" in "${WORK_PARENT}"/*) ;; *) fail "unsafe work directory" ;; esac
[[ -d "${WORK_ROOT}" && ! -L "${WORK_ROOT}" ]] || \
    fail "could not create a safe Linux-native work directory"
mkdir -p -- "${OUTPUT_DIR}"

python3 -m venv "${WORK_ROOT}/venv"
PYTHON="${WORK_ROOT}/venv/bin/python"
"${PYTHON}" -m pip install --disable-pip-version-check \
    -r "${PROJECT_ROOT}/agent/requirements.txt" \
    -r "${SCRIPT_ROOT}/requirements-test.txt" \
    -r "${SCRIPT_ROOT}/requirements-build.txt"
"${PYTHON}" -m pip check

if [[ "${RIGPILOT_SKIP_TESTS:-0}" != "1" ]]; then
    (cd -- "${PROJECT_ROOT}" && "${PYTHON}" -m pytest -q agent/tests)
fi

PYTHONHASHSEED=0 SOURCE_DATE_EPOCH=0 "${PYTHON}" -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --noupx \
    --name RigPilotAgent \
    --paths "${PROJECT_ROOT}" \
    --distpath "${WORK_ROOT}/dist" \
    --workpath "${WORK_ROOT}/pyinstaller" \
    --specpath "${WORK_ROOT}/spec" \
    "${SCRIPT_ROOT}/linux_agent_entry.py"

AGENT_BINARY="${WORK_ROOT}/dist/RigPilotAgent"
[[ -f "${AGENT_BINARY}" && ! -L "${AGENT_BINARY}" ]] || \
    fail "frozen RigPilotAgent executable is missing or unsafe"
chmod 0755 "${AGENT_BINARY}"
ACTUAL_VERSION="$("${AGENT_BINARY}" --version)"
[[ "${ACTUAL_VERSION}" == "RigPilot Agent ${VERSION}" ]] || \
    fail "frozen version self-check failed: ${ACTUAL_VERSION}"

PORTABLE="${WORK_ROOT}/RigPilotAgent-Ubuntu-x64"
mkdir -p -- "${PORTABLE}"
install -m 0755 "${AGENT_BINARY}" "${PORTABLE}/RigPilotAgent"
for asset in Install.sh Start-Portable.sh Remove-Systemd.sh RigPilotAgentLauncher.sh; do
    install -m 0755 "${SCRIPT_ROOT}/portable/${asset}" "${PORTABLE}/${asset}"
done
install -m 0755 "${SCRIPT_ROOT}/portable/RigPilotAgentUpdater.py" \
    "${PORTABLE}/RigPilotAgentUpdater.py"
install -m 0644 "${SCRIPT_ROOT}/portable/rigpilot-agent.service" \
    "${PORTABLE}/rigpilot-agent.service"
install -m 0644 "${SCRIPT_ROOT}/portable/rigpilot-agent-supervision.conf" \
    "${PORTABLE}/rigpilot-agent-supervision.conf"
install -m 0644 "${SCRIPT_ROOT}/portable/rigpilot-agent-update-recovery.service" \
    "${PORTABLE}/rigpilot-agent-update-recovery.service"
install -m 0644 "${SCRIPT_ROOT}/portable/rigpilot-agent-update-recovery.timer" \
    "${PORTABLE}/rigpilot-agent-update-recovery.timer"
# README-Ubuntu.txt is a %%VERSION%% template.  The package version is
# injected here and asserted right after rendering, so a stale hardcoded
# version declaration can never ship again (it drifted once across
# 0.8.58/0.8.59).  Tests re-run this exact rendering path.
template_occurrences="$(grep -c '%%VERSION%%' -- "${SCRIPT_ROOT}/portable/README-Ubuntu.txt" || true)"
[[ "${template_occurrences}" == "1" ]] || \
    fail "README-Ubuntu.txt template must contain exactly one %%VERSION%% token"
sed -e "s/%%VERSION%%/${VERSION}/g" -- "${SCRIPT_ROOT}/portable/README-Ubuntu.txt" \
    >"${PORTABLE}/README-Ubuntu.txt"
chmod 0644 "${PORTABLE}/README-Ubuntu.txt"
IFS= read -r readme_first_line <"${PORTABLE}/README-Ubuntu.txt"
[[ "${readme_first_line}" == "RigPilot Agent ${VERSION} for Ubuntu x86_64" ]] || \
    fail "README-Ubuntu.txt was not rendered for version ${VERSION}"
if grep -q '%%VERSION%%' -- "${PORTABLE}/README-Ubuntu.txt"; then
    fail "README-Ubuntu.txt still contains an unresolved version template token"
fi
printf '%s\n' "${VERSION}" >"${PORTABLE}/VERSION"
{
    printf 'build_baseline=Ubuntu 22.04 / glibc <= 2.35 / x86_64\n'
    printf 'python=%s\n' "$("${PYTHON}" --version 2>&1)"
    "${PYTHON}" -m pip freeze --all | LC_ALL=C sort
} >"${PORTABLE}/BUILD-DEPENDENCIES.txt"

# Windows checkouts and editors may leave release assets as CRLF.  Normalize the
# staged Linux payload before hashing it, then reject any remaining carriage
# return so shell scripts, systemd units and configuration files are portable.
normalize_linux_text_tree "${PORTABLE}"

write_release_manifest() {
    local root="$1"
    PYTHONPATH="${PROJECT_ROOT}" "${PYTHON}" - "${root}" "${VERSION}" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
version = sys.argv[2]
files = {}
for path in sorted(root.rglob("*")):
    if path.is_file() and not path.is_symlink():
        relative = path.relative_to(root).as_posix()
        if relative in {"agent-release.json", "SHA256SUMS"}:
            continue
        files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
manifest = {
    "schema_version": 1,
    "version": version,
    "platform": "linux",
    "architecture": "x86_64",
    "files": files,
}
(root / "agent-release.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY
}

write_release_metadata() {
    local root="$1"
    rm -f -- "${root}/agent-release.json" "${root}/SHA256SUMS"
    write_release_manifest "${root}"
    # SHA256SUMS binds both the payload and agent-release.json.  It omits only
    # itself, while the manifest independently binds the payload and omits
    # both metadata files.  This has no circular digest and both file sets can
    # be checked exactly by Install.sh/the fixed updater.
    (cd -- "${root}" && find . -type f ! -name SHA256SUMS -printf '%P\0' \
        | LC_ALL=C sort -z | xargs -0 sha256sum >SHA256SUMS)
    (cd -- "${root}" && sha256sum --check --strict SHA256SUMS)
}

write_release_metadata "${PORTABLE}"
verify_linux_text_tree_lf "${PORTABLE}"

verify_staged_modes() {
    local root="$1"
    PYTHONPATH="${PROJECT_ROOT}" "${PYTHON}" - "${root}" <<'PY'
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
executables = {
    "Install.sh",
    "Remove-Systemd.sh",
    "RigPilotAgent",
    "RigPilotAgentLauncher.sh",
    "RigPilotAgentUpdater.py",
    "Start-Portable.sh",
}
for path in sorted([root, *root.rglob("*")]):
    relative = path.relative_to(root).as_posix() if path != root else "."
    mode = stat.S_IMODE(path.stat().st_mode)
    expected = 0o755 if path.is_dir() or relative in executables else 0o644
    if mode != expected:
        raise SystemExit(
            f"unsafe staged mode for {relative}: {mode:04o}, expected {expected:04o}"
        )
PY
}

verify_staged_modes "${PORTABLE}"

publish_release_pair() {
    # Same-directory links publish without replacing a concurrent build's
    # output. Once public, preserve the archive even if its sidecar fails.
    "${PYTHON}" - "$1" "$2" "$3" <<'PY'
import os
import sys

stage, archive, hash_stage = sys.argv[1:]
try:
    os.link(stage, archive)
except OSError as exc:
    raise SystemExit(f"release archive already exists or cannot be published: {archive}: {exc}")
try:
    os.link(hash_stage, archive + ".sha256")
except OSError as exc:
    raise SystemExit(f"archive published but SHA-256 sidecar could not be published: {archive}: {exc}")
os.unlink(stage)
os.unlink(hash_stage)
PY
}

ARCHIVE="${OUTPUT_DIR}/RigPilotAgent-${VERSION}-Ubuntu-x86_64.tar.gz"
[[ ! -e "${ARCHIVE}" && ! -L "${ARCHIVE}" && \
    ! -e "${ARCHIVE}.sha256" && ! -L "${ARCHIVE}.sha256" ]] || \
    fail "refusing to overwrite an existing Ubuntu release: ${ARCHIVE}"
ARCHIVE_STAGE="${ARCHIVE}.part.$$"
ARCHIVE_HASH_STAGE="${ARCHIVE}.sha256.part.$$"
tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
    -C "${WORK_ROOT}" -czf "${ARCHIVE_STAGE}" "$(basename -- "${PORTABLE}")"
[[ -s "${ARCHIVE_STAGE}" ]] || fail "release archive was not created"
# Consume the completed gzip/tar stream once before reporting its outer hash.
# This catches a truncated or otherwise unreadable local release artifact.
tar -tzf "${ARCHIVE_STAGE}" >/dev/null
verify_linux_archive_text_lf "${ARCHIVE_STAGE}"
ARCHIVE_SHA256="$(sha256sum "${ARCHIVE_STAGE}" | awk '{print $1}')"
printf '%s  %s\n' "${ARCHIVE_SHA256}" "$(basename -- "${ARCHIVE}")" \
    >"${ARCHIVE_HASH_STAGE}"
publish_release_pair "${ARCHIVE_STAGE}" "${ARCHIVE}" "${ARCHIVE_HASH_STAGE}"
ARCHIVE_STAGE=""
ARCHIVE_HASH_STAGE=""

# The controller accepts this minimal online payload.  It deliberately omits
# Install.sh and both trust anchors: agent_update.v1 may install only the new
# version-slotted binary and can never replace its own update mechanism.
ONLINE="${WORK_ROOT}/RigPilotAgent-Ubuntu-x64-OnlineUpdate"
mkdir -p -- "${ONLINE}"
install -m 0755 "${AGENT_BINARY}" "${ONLINE}/RigPilotAgent"
normalize_linux_text_tree "${ONLINE}"
write_release_manifest "${ONLINE}"
verify_linux_text_tree_lf "${ONLINE}"
verify_staged_modes "${ONLINE}"
[[ "$(find "${ONLINE}" -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort | tr '\n' ' ')" == \
    "RigPilotAgent agent-release.json " ]] || fail "online payload contains unexpected files"
ONLINE_ARCHIVE="${OUTPUT_DIR}/RigPilotAgent-${VERSION}-Ubuntu-x86_64-OnlineUpdate.tar.gz"
[[ ! -e "${ONLINE_ARCHIVE}" && ! -L "${ONLINE_ARCHIVE}" && \
    ! -e "${ONLINE_ARCHIVE}.sha256" && ! -L "${ONLINE_ARCHIVE}.sha256" ]] || \
    fail "refusing to overwrite an existing Ubuntu release: ${ONLINE_ARCHIVE}"
ONLINE_STAGE="${ONLINE_ARCHIVE}.part.$$"
ONLINE_HASH_STAGE="${ONLINE_ARCHIVE}.sha256.part.$$"
tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
    -C "${WORK_ROOT}" -czf "${ONLINE_STAGE}" "$(basename -- "${ONLINE}")"
[[ -s "${ONLINE_STAGE}" ]] || fail "online update archive was not created"
tar -tzf "${ONLINE_STAGE}" >/dev/null
verify_linux_archive_text_lf "${ONLINE_STAGE}"
ONLINE_SHA256="$(sha256sum "${ONLINE_STAGE}" | awk '{print $1}')"
printf '%s  %s\n' "${ONLINE_SHA256}" "$(basename -- "${ONLINE_ARCHIVE}")" \
    >"${ONLINE_HASH_STAGE}"
publish_release_pair "${ONLINE_STAGE}" "${ONLINE_ARCHIVE}" "${ONLINE_HASH_STAGE}"
ONLINE_STAGE=""
ONLINE_HASH_STAGE=""

# Verify both completed archives before reporting a successful leaf build.
"${PYTHON}" "${PROJECT_ROOT}/deploy/verify-release-artifacts.py" \
    --output-dir "${OUTPUT_DIR}" --agent-version "${VERSION}" \
    --skip-windows --skip-controller --smoke-native none

printf 'Manual package: %s\nManual SHA-256: %s\n' "${ARCHIVE}" "${ARCHIVE_SHA256}"
printf 'Online payload: %s\nOnline SHA-256: %s\n' "${ONLINE_ARCHIVE}" "${ONLINE_SHA256}"
