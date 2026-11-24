#!/usr/bin/env bash
set -Eeuo pipefail

# Capture explicit one-time codes before any package/configuration helper can
# inherit them. Only the selected Agent --once process receives one code.
PORTABLE_ENROLLMENT_TOKEN_SET=0
PORTABLE_REPLACEMENT_TOKEN_SET=0
PORTABLE_ENROLLMENT_TOKEN=""
PORTABLE_REPLACEMENT_TOKEN=""
export -n PORTABLE_ENROLLMENT_TOKEN_SET PORTABLE_REPLACEMENT_TOKEN_SET \
    PORTABLE_ENROLLMENT_TOKEN PORTABLE_REPLACEMENT_TOKEN
if [[ ${RIGPILOT_ENROLLMENT_TOKEN+x} ]]; then
    PORTABLE_ENROLLMENT_TOKEN_SET=1
    PORTABLE_ENROLLMENT_TOKEN="${RIGPILOT_ENROLLMENT_TOKEN}"
fi
if [[ ${RIGPILOT_REPLACEMENT_TOKEN+x} ]]; then
    PORTABLE_REPLACEMENT_TOKEN_SET=1
    PORTABLE_REPLACEMENT_TOKEN="${RIGPILOT_REPLACEMENT_TOKEN}"
fi
unset RIGPILOT_ENROLLMENT_TOKEN RIGPILOT_REPLACEMENT_TOKEN

readonly CONTROLLER_DEFAULT="https://controller.example.invalid"

fail() {
    printf 'RigPilot Agent portable start failed: %s\n' "$*" >&2
    exit 1
}

# Keep the installer and foreground entry on the same identity protocol.
# This local adaptation avoids adding a new required release-package member.
prepare_existing_identity_config() {
    # A saved identity owns its Controller and device metadata. A pending
    # claim likewise owns its original binding; only a claim with no journal
    # may correct a previous pre-claim configuration mistake.
    if [[ -s "${STATE}" ]]; then
        [[ -e "${CONFIG}" ]] || fail "saved identity has no Agent configuration; preserve the Agent data"
        return
    fi
    local enrollment_pending="${STATE%/*}/.${STATE##*/}.enrollment-pending"
    local replacement_pending="${STATE%/*}/.${STATE##*/}.replacement-pending"
    local has_pending=0
    if [[ -e "${enrollment_pending}" || -L "${enrollment_pending}" ||
          -e "${replacement_pending}" || -L "${replacement_pending}" ]]; then
        has_pending=1
        [[ -e "${CONFIG}" ]] ||
            fail "pending registration or inheritance has no Agent configuration; preserve the Agent data"
    fi
    [[ -e "${CONFIG}" ]] || return 0
    if [[ ! ${RIGPILOT_CONTROLLER_URL+x} && ! ${RIGPILOT_DEVICE_NAME+x} ]]; then
        return
    fi

    # Environment codes were already removed. Metadata is read in Python from
    # the environment, so even a malformed URL is not copied into argv/logs.
    python3 - "${CONFIG}" "${has_pending}" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

path = Path(sys.argv[1])
pending = sys.argv[2] == "1"
url_supplied = "RIGPILOT_CONTROLLER_URL" in os.environ
name_supplied = "RIGPILOT_DEVICE_NAME" in os.environ
url = os.environ.get("RIGPILOT_CONTROLLER_URL", "")
name = os.environ.get("RIGPILOT_DEVICE_NAME", "")
if url_supplied:
    try:
        parsed = urlsplit(url)
        valid = (url.startswith("https://") and not any(c.isspace() or c == "\\" for c in url)
                 and parsed.scheme == "https" and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit("RIGPILOT_CONTROLLER_URL must be a credential-free HTTPS URL")
if name_supplied and (not name.strip() or len(name) > 80):
    raise SystemExit("RIGPILOT_DEVICE_NAME must contain a visible character and be at most 80 characters")
try:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
except (OSError, UnicodeError, ValueError):
    raise SystemExit("existing Agent configuration cannot be read") from None
if not isinstance(value, dict):
    raise SystemExit("existing Agent configuration must be an object")
current_url = value.get("server_url")
current_name = value.get("name")
if pending:
    if ((url_supplied and url != current_url)
            or (name_supplied and name != (current_name or ""))):
        raise SystemExit("pending registration or inheritance is bound to its existing Controller URL and device name")
    raise SystemExit(0)
if ((not url_supplied or url == current_url)
        and (not name_supplied or name == current_name)):
    raise SystemExit(0)
if url_supplied:
    value["server_url"] = url
if name_supplied:
    value["name"] = name
# Only explicitly supplied pre-claim metadata changes; saved identities
# and pending claims returned above without modifying their configuration.
fd, temporary = tempfile.mkstemp(prefix=".config.json.preclaim.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

enroll_agent_identity() {
    local pending_enrollment_file="${STATE%/*}/.${STATE##*/}.enrollment-pending"
    local pending_replacement_file="${STATE%/*}/.${STATE##*/}.replacement-pending"
    local enrollment_status=0
    local enrollment_env_set="${PORTABLE_ENROLLMENT_TOKEN_SET}" replacement_env_set="${PORTABLE_REPLACEMENT_TOKEN_SET}" token_ready=0
    local mode="" choice="" ENROLLMENT_TOKEN="" REPLACEMENT_TOKEN=""
    local enrollment_pending=0 replacement_pending=0
    export -n ENROLLMENT_TOKEN REPLACEMENT_TOKEN

    [[ -e "${pending_enrollment_file}" || -L "${pending_enrollment_file}" ]] && enrollment_pending=1 || true
    [[ -e "${pending_replacement_file}" || -L "${pending_replacement_file}" ]] && replacement_pending=1 || true
    if [[ "${enrollment_pending}" -eq 1 && "${replacement_pending}" -eq 1 ]]; then
        fail "both registration and inheritance are pending; preserve the Agent data and resolve the conflicting journals before retrying"
    fi
    if [[ "${enrollment_env_set}" -eq 1 && "${replacement_env_set}" -eq 1 ]]; then
        fail "enrollment and inheritance codes cannot be supplied together"
    fi

    if [[ "${enrollment_pending}" -eq 1 ]]; then
        mode="new"
        [[ "${replacement_env_set}" -eq 0 ]] ||
            fail "registration is pending; retry only its original enrollment code"
        if [[ "${enrollment_env_set}" -eq 1 ]]; then
            ENROLLMENT_TOKEN="${PORTABLE_ENROLLMENT_TOKEN}"
            token_ready=1
        else
            # A completed registration can be recovered without the original
            # code. The packaged Agent checks the private journal and binding.
            printf 'Retrying the pending registration from the existing Agent directory.\n'
            if ( unset RIGPILOT_ENROLLMENT_TOKEN RIGPILOT_REPLACEMENT_TOKEN
                 exec "${ROOT}/RigPilotAgent" --config "${CONFIG}" --once ); then
                enrollment_status=0
            else
                enrollment_status=$?
            fi
            if [[ "${enrollment_status}" -ne 0 && ! -s "${STATE}" && -t 0 ]] &&
                [[ -e "${pending_enrollment_file}" || -L "${pending_enrollment_file}" ]]; then
                # Only the original code may complete an attempt that never
                # reached the Controller. Never create a fresh request here.
                printf 'Original enrollment code (must be the same code; leave blank to exit): '
                IFS= read -r -s ENROLLMENT_TOKEN || ENROLLMENT_TOKEN=""
                printf '\n'
                [[ -z "${ENROLLMENT_TOKEN}" ]] || token_ready=1
            fi
        fi
    elif [[ "${replacement_pending}" -eq 1 ]]; then
        mode="inherit"
        [[ "${enrollment_env_set}" -eq 0 ]] ||
            fail "inheritance is pending; retry only its original inheritance code"
        if [[ "${replacement_env_set}" -eq 1 ]]; then
            REPLACEMENT_TOKEN="${PORTABLE_REPLACEMENT_TOKEN}"
        else
            [[ -t 0 ]] ||
                fail "pending inheritance requires the original inheritance code; preserve the Agent data and retry with RIGPILOT_REPLACEMENT_TOKEN"
            printf 'Original inheritance code (must be the same code): '
            IFS= read -r -s REPLACEMENT_TOKEN || REPLACEMENT_TOKEN=""
            printf '\n'
        fi
        token_ready=1
    else
        if [[ "${enrollment_env_set}" -eq 1 ]]; then
            mode="new"
            ENROLLMENT_TOKEN="${PORTABLE_ENROLLMENT_TOKEN}"
        elif [[ "${replacement_env_set}" -eq 1 ]]; then
            mode="inherit"
            REPLACEMENT_TOKEN="${PORTABLE_REPLACEMENT_TOKEN}"
        else
            [[ -t 0 ]] ||
                fail "first registration needs an interactive choice or exactly one enrollment/inheritance code in the environment"
            printf 'Registration type: [1] New device (default), [2] Inherit existing device: '
            IFS= read -r choice || fail "registration type could not be read"
            case "${choice}" in
                ''|1) mode="new" ;;
                2) mode="inherit" ;;
                *) fail "choose 1 for a new device or 2 to inherit an existing device" ;;
            esac
            if [[ "${mode}" == "new" ]]; then
                printf 'One-time enrollment code: '
                IFS= read -r -s ENROLLMENT_TOKEN || fail "the enrollment code could not be read"
            else
                printf 'Device inheritance code: '
                IFS= read -r -s REPLACEMENT_TOKEN || fail "the inheritance code could not be read"
            fi
            printf '\n'
        fi
        token_ready=1
    fi

    if [[ "${token_ready}" -eq 1 && "${mode}" == "new" ]]; then
        [[ ${#ENROLLMENT_TOKEN} -ge 20 && ${#ENROLLMENT_TOKEN} -le 256 ]] ||
            fail "the enrollment code must contain between 20 and 256 characters; preserve any pending registration and retry with its original code"
        if ( unset RIGPILOT_REPLACEMENT_TOKEN
             export RIGPILOT_ENROLLMENT_TOKEN="${ENROLLMENT_TOKEN}"
             exec "${ROOT}/RigPilotAgent" --config "${CONFIG}" --once ); then
            enrollment_status=0
        else
            enrollment_status=$?
        fi
    elif [[ "${token_ready}" -eq 1 && "${mode}" == "inherit" ]]; then
        [[ "${REPLACEMENT_TOKEN}" =~ ^rpr_[A-Za-z0-9_-]{43}$ ]] ||
            fail "the inheritance code is invalid; preserve any pending inheritance and retry with its original code"
        if ( unset RIGPILOT_ENROLLMENT_TOKEN
             export RIGPILOT_REPLACEMENT_TOKEN="${REPLACEMENT_TOKEN}"
             exec "${ROOT}/RigPilotAgent" --config "${CONFIG}" --once ); then
            enrollment_status=0
        else
            enrollment_status=$?
        fi
    fi
    unset ENROLLMENT_TOKEN REPLACEMENT_TOKEN
    if [[ "${enrollment_status}" -ne 0 ]]; then
        # Exit 10 is reserved for a bound, definitive ordinary enrollment
        # rejection after the Agent durably removed its journal.
        if [[ "${mode}" == "new" && "${enrollment_status}" -eq 10 &&
              ! -e "${pending_enrollment_file}" && ! -L "${pending_enrollment_file}" ]]; then
            fail "the Controller confirmed registration was not created; use a valid new one-time code when rerunning Start-Portable.sh"
        fi
        if [[ "${mode}" == "inherit" ]]; then
            fail "device inheritance could not be confirmed; preserve this Agent directory and retry with the original inheritance code, never a new code"
        fi
        fail "registration could not be confirmed; preserve this Agent directory and retry Start-Portable.sh with the original enrollment code"
    fi
    [[ -s "${STATE}" ]] ||
        fail "identity was not saved; preserve this Agent directory and retry Start-Portable.sh with the original code"
}

[[ "$(uname -s)" == "Linux" ]] || fail "Linux is required"
case "$(uname -m)" in x86_64|amd64) ;; *) fail "x86_64 is required" ;; esac
[[ "${EUID}" -eq 0 ]] || fail "run with sudo for mining, GPU and power actions"

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA="${ROOT}/data"
CONFIG="${DATA}/config.json"
STATE="${DATA}/state.json"
[[ -f "${ROOT}/RigPilotAgent" && ! -L "${ROOT}/RigPilotAgent" ]] || \
    fail "RigPilotAgent executable is missing or unsafe"
if [[ -f "${ROOT}/SHA256SUMS" ]]; then
    (cd -- "${ROOT}" && sha256sum --check --strict SHA256SUMS) || \
        fail "package integrity verification failed"
fi
install -d -m 0700 "${DATA}" "${DATA}/logs" "${DATA}/miner-runtime" "${DATA}/packages"

if [[ ! -s "${STATE}" && "${PORTABLE_ENROLLMENT_TOKEN_SET}" -eq 1 &&
      "${PORTABLE_REPLACEMENT_TOKEN_SET}" -eq 1 ]]; then
    fail "enrollment and inheritance codes cannot be supplied together"
fi
prepare_existing_identity_config

if [[ ! -e "${CONFIG}" ]]; then
    command -v python3 >/dev/null 2>&1 || fail "python3 is required for first-run configuration"
    # A pre-supplied code selects unattended first registration. Require its
    # Controller and visible device name explicitly, so a media/bootstrap typo
    # cannot silently claim a device against the default production endpoint.
    if [[ "${PORTABLE_ENROLLMENT_TOKEN_SET}" -eq 1 || "${PORTABLE_REPLACEMENT_TOKEN_SET}" -eq 1 ]]; then
        [[ -n "${RIGPILOT_CONTROLLER_URL:-}" && -n "${RIGPILOT_DEVICE_NAME:-}" ]] ||
            fail "unattended registration requires RIGPILOT_CONTROLLER_URL and RIGPILOT_DEVICE_NAME"
        CONTROLLER="${RIGPILOT_CONTROLLER_URL}"
        DEVICE_NAME="${RIGPILOT_DEVICE_NAME}"
        [[ "${DEVICE_NAME}" =~ [^[:space:]] ]] ||
            fail "RIGPILOT_DEVICE_NAME must contain a visible character"
    elif [[ -t 0 ]]; then
        printf 'Controller URL [example: %s]: ' "${CONTROLLER_DEFAULT}"
        IFS= read -r CONTROLLER
        [[ -n "${CONTROLLER}" && "${CONTROLLER}" != "${CONTROLLER_DEFAULT}" ]] || fail "Enter your Controller HTTPS URL"
        printf 'Device remark/name (optional): '
        IFS= read -r DEVICE_NAME
    else
        fail "first portable start needs an interactive terminal or one enrollment/inheritance code with explicit Controller URL and device name"
    fi
    [[ "${CONTROLLER}" =~ ^https://[^[:space:]/]+(:[0-9]+)?(/[^[:space:]]*)?$ ]] || \
        fail "Controller URL must be an HTTPS URL without credentials"
    [[ ${#DEVICE_NAME} -le 80 ]] || fail "device name is too long"
    python3 - "${CONFIG}" "${DATA}" "${CONTROLLER}" "${DEVICE_NAME}" <<'PY'
import json
import os
import sys
from urllib.parse import urlparse

target, data, controller, name = sys.argv[1:]
try:
    parsed = urlparse(controller)
    valid = (
        parsed.scheme.lower() == "https" and parsed.hostname
        and parsed.username is None and parsed.password is None
        and not parsed.query and not parsed.fragment and parsed.port != 0
        and "\\" not in controller
    )
except ValueError:
    valid = False
if not valid:
    raise SystemExit("Controller URL must be a credential-free HTTPS URL")
value = {
    "server_url": controller,
    "state_file": os.path.join(data, "state.json"),
    "name": name or None,
    "heartbeat_interval": 30,
    "request_timeout": 20,
    "log_level": "INFO",
    "log_file": os.path.join(data, "logs", "agent.jsonl"),
    "allow_reboot": True,
    "allow_shutdown": True,
    "allow_agent_updates": False,
    "collect_gpu_metrics": True,
    # Legacy direct-connection metadata stays disabled.  Structured remote
    # access uses only the Agent's existing outbound authenticated HTTPS channel.
    "advertise_ssh": True,
    "advertise_sftp": True,
    "ssh_port": 22,
    "ssh_username": None,
    "flight_sheet_file": os.path.join(data, "flight-sheet.json"),
    "miner_status_file": os.path.join(data, "miner-runtime", "miner-status.json"),
    "managed_mining_enabled": True,
    "managed_miner_runtime_dir": os.path.join(data, "miner-runtime"),
    "miner_profile_allowlist": [],
    "flight_sheet_extra_key_allowlist": [],
    "allow_miner_downloads": True,
    "miner_packages_dir": os.path.join(data, "packages"),
    "miner_download_host_allowlist": [],
    "allow_gpu_oc": True,
    "gpu_oc_allowed_vendors": ["nvidia"],
}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
fi

if [[ ! -s "${STATE}" ]]; then
    enroll_agent_identity
fi
unset PORTABLE_ENROLLMENT_TOKEN PORTABLE_REPLACEMENT_TOKEN
exec "${ROOT}/RigPilotAgent" --config "${CONFIG}"
