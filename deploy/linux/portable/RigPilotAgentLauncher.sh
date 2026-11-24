#!/usr/bin/env bash
set -Eeuo pipefail

# This launcher is a protocol-v1 trust anchor.  Online releases install only
# a version-slotted Agent binary; they never replace or execute a launcher
# shipped inside the downloaded bundle.
readonly APP_DIR="/opt/rigpilot-agent"
readonly VERSIONS_DIR="${APP_DIR}/versions"
readonly ACTIVE_VERSION_FILE="${APP_DIR}/active-version"

fail() {
    printf 'RigPilot Agent launcher failed: %s\n' "$*" >&2
    exit 1
}

plain_root_file() {
    local path="$1"
    local label="$2"
    [[ -f "${path}" && ! -L "${path}" ]] || fail "${label} is missing or unsafe"
    [[ "$(stat -c '%u' -- "${path}")" == "0" ]] || fail "${label} is not owned by root"
    local mode
    mode="$(stat -c '%a' -- "${path}")"
    (( (8#${mode} & 8#022) == 0 )) || fail "${label} is writable by group or others"
}

plain_root_directory() {
    local path="$1"
    local label="$2"
    [[ -d "${path}" && ! -L "${path}" ]] || fail "${label} is missing or unsafe"
    [[ "$(stat -c '%u' -- "${path}")" == "0" ]] || fail "${label} is not owned by root"
    local mode
    mode="$(stat -c '%a' -- "${path}")"
    (( (8#${mode} & 8#022) == 0 )) || fail "${label} is writable by group or others"
}

plain_root_directory "${APP_DIR}" "installation root"
plain_root_directory "${VERSIONS_DIR}" "versions directory"
plain_root_file "${ACTIVE_VERSION_FILE}" "active version pointer"
[[ "$(wc -l <"${ACTIVE_VERSION_FILE}")" -eq 1 ]] || fail "active version pointer is invalid"
IFS= read -r ACTIVE_VERSION <"${ACTIVE_VERSION_FILE}" || fail "cannot read active version"
[[ "${ACTIVE_VERSION}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || \
    fail "active version pointer is invalid"
[[ "$(cat -- "${ACTIVE_VERSION_FILE}")" == "${ACTIVE_VERSION}" ]] || \
    fail "active version pointer contains unexpected data"

SLOT_DIR="${VERSIONS_DIR}/${ACTIVE_VERSION}"
AGENT="${SLOT_DIR}/RigPilotAgent"
plain_root_directory "${SLOT_DIR}" "active version slot"
plain_root_file "${AGENT}" "active Agent executable"
[[ -x "${AGENT}" ]] || fail "active Agent executable is not executable"

RESOLVED_VERSIONS="$(realpath -e -- "${VERSIONS_DIR}")"
RESOLVED_AGENT="$(realpath -e -- "${AGENT}")"
case "${RESOLVED_AGENT}" in
    "${RESOLVED_VERSIONS}"/*/RigPilotAgent) ;;
    *) fail "active Agent executable escapes the versions directory" ;;
esac

exec "${RESOLVED_AGENT}" "$@"
