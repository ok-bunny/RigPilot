#!/usr/bin/env bash
set -Eeuo pipefail

# Capture explicit one-time codes before any package/configuration helper can
# inherit them. Only the selected Agent --once process receives one code.
INSTALL_ENROLLMENT_TOKEN_SET=0
INSTALL_REPLACEMENT_TOKEN_SET=0
INSTALL_ENROLLMENT_TOKEN=""
INSTALL_REPLACEMENT_TOKEN=""
export -n INSTALL_ENROLLMENT_TOKEN_SET INSTALL_REPLACEMENT_TOKEN_SET \
    INSTALL_ENROLLMENT_TOKEN INSTALL_REPLACEMENT_TOKEN
if [[ ${RIGPILOT_ENROLLMENT_TOKEN+x} ]]; then
    INSTALL_ENROLLMENT_TOKEN_SET=1
    INSTALL_ENROLLMENT_TOKEN="${RIGPILOT_ENROLLMENT_TOKEN}"
fi
if [[ ${RIGPILOT_REPLACEMENT_TOKEN+x} ]]; then
    INSTALL_REPLACEMENT_TOKEN_SET=1
    INSTALL_REPLACEMENT_TOKEN="${RIGPILOT_REPLACEMENT_TOKEN}"
fi
unset RIGPILOT_ENROLLMENT_TOKEN RIGPILOT_REPLACEMENT_TOKEN

readonly CONTROLLER_DEFAULT="https://controller.example.invalid"
readonly APP_DIR="/opt/rigpilot-agent"
readonly VERSIONS_DIR="${APP_DIR}/versions"
readonly ACTIVE_VERSION_FILE="${APP_DIR}/active-version"
readonly CONFIG_DIR="/etc/rigpilot-agent"
readonly DATA_DIR="/var/lib/rigpilot-agent"
readonly CONFIG_FILE="${CONFIG_DIR}/config.json"
readonly STATE_FILE="${DATA_DIR}/state.json"
readonly SERVICE_FILE="/etc/systemd/system/rigpilot-agent.service"
readonly SERVICE_DROPIN_DIR="/etc/systemd/system/rigpilot-agent.service.d"
readonly SUPERVISION_DROPIN="${SERVICE_DROPIN_DIR}/supervision.conf"
readonly REMOTE_SESSION_DROPIN="${SERVICE_DROPIN_DIR}/remote-sessions.conf"
readonly LEGACY_WIFI_DROPIN="${SERVICE_DROPIN_DIR}/wifi-management.conf"
readonly RECOVERY_SERVICE="/etc/systemd/system/rigpilot-agent-update-recovery.service"
readonly RECOVERY_TIMER="/etc/systemd/system/rigpilot-agent-update-recovery.timer"
# Single source of truth for the LAN SSH drop-in first line.  Remove-Systemd.sh
# carries the identical constant plus the earlier Spark wording it still
# accepts; tests/test_release_scripts.py asserts both files stay aligned.
readonly LAN_SSH_DROPIN_MARKER="# Managed by RigPilot: password SSH is LAN-only; the Agent does not use port 22."

fail() {
    printf 'RigPilot Agent installation failed: %s\n' "$*" >&2
    exit 1
}

warn_registered_agent_offline() {
    [[ -s "${STATE_FILE:-}" && -f "${CONFIG_FILE:-}" ]] || return 0
    systemctl is-active --quiet rigpilot-agent.service 2>/dev/null && return 0
    printf 'This device has a saved identity but is NOT online: the Agent service is not running.\n' >&2
    printf 'For temporary foreground use with the same saved identity, run:\n' >&2
    printf '  sudo %q --config %q\n' "${SOURCE_ROOT}/RigPilotAgent" "${CONFIG_FILE}" >&2
    printf 'Keep that terminal open. Fix the installation error and rerun Install.sh to restore background startup.\n' >&2
    printf 'Do not use Start-Portable.sh for this recovery: it uses a separate identity in the extracted data directory.\n' >&2
}

check_legacy_wifi_dropin() {
    # An older Agent package granted Netplan write access through this file.
    # Remove only the exact root-owned file that package installed. A changed
    # or unsafe file needs manual review and must not be silently overwritten.
    [[ -e "${LEGACY_WIFI_DROPIN}" || -L "${LEGACY_WIFI_DROPIN}" ]] || return 0
    [[ -f "${LEGACY_WIFI_DROPIN}" && ! -L "${LEGACY_WIFI_DROPIN}" ]] ||
        fail "unsafe legacy systemd drop-in: ${LEGACY_WIFI_DROPIN}"
    [[ "$(stat -c '%u:%g:%a:%h' -- "${LEGACY_WIFI_DROPIN}")" == "0:0:644:1" ]] ||
        fail "legacy systemd drop-in has unexpected ownership or mode: ${LEGACY_WIFI_DROPIN}"
    local digest=""
    read -r digest _ < <(sha256sum -- "${LEGACY_WIFI_DROPIN}")
    [[ "${digest}" == "7be43f800013a312bb3ed6e46948e8c003d4e527661e2be8075bfd43c294c9fb" ]] ||
        fail "legacy systemd drop-in has changed: ${LEGACY_WIFI_DROPIN}"
}

prepare_existing_identity_config() {
    # A saved identity owns its Controller and device metadata. A pending
    # claim likewise owns its original binding; only a claim with no journal
    # may correct a previous pre-claim configuration mistake.
    if [[ -s "${STATE_FILE}" ]]; then
        [[ -e "${CONFIG_FILE}" ]] || fail "saved identity has no Agent configuration; preserve the Agent data"
        return
    fi
    local enrollment_pending="${STATE_FILE%/*}/.${STATE_FILE##*/}.enrollment-pending"
    local replacement_pending="${STATE_FILE%/*}/.${STATE_FILE##*/}.replacement-pending"
    local has_pending=0
    if [[ -e "${enrollment_pending}" || -L "${enrollment_pending}" ||
          -e "${replacement_pending}" || -L "${replacement_pending}" ]]; then
        has_pending=1
        [[ -e "${CONFIG_FILE}" ]] ||
            fail "pending registration or inheritance has no Agent configuration; preserve the Agent data"
    fi
    [[ -e "${CONFIG_FILE}" ]] || return 0
    if [[ ! ${RIGPILOT_CONTROLLER_URL+x} && ! ${RIGPILOT_DEVICE_NAME+x} ]]; then
        return
    fi

    # Environment codes were already removed. Metadata is read in Python from
    # the environment, so even a malformed URL is not copied into argv/logs.
    python3 - "${CONFIG_FILE}" "${has_pending}" <<'PY'
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
# The existing-config migration below stages from this corrected file. Its
# later rollback snapshot is therefore also bound to the corrected metadata.
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
    local pending_enrollment_file="${STATE_FILE%/*}/.${STATE_FILE##*/}.enrollment-pending"
    local pending_replacement_file="${STATE_FILE%/*}/.${STATE_FILE##*/}.replacement-pending"
    local enrollment_status=0
    local enrollment_env_set="${INSTALL_ENROLLMENT_TOKEN_SET}" replacement_env_set="${INSTALL_REPLACEMENT_TOKEN_SET}" token_ready=0
    local mode="" choice="" ENROLLMENT_TOKEN="" REPLACEMENT_TOKEN=""
    local enrollment_pending=0 replacement_pending=0

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
            ENROLLMENT_TOKEN="${INSTALL_ENROLLMENT_TOKEN}"
            token_ready=1
        else
            # A completed registration can be recovered without the original
            # code. The packaged Agent checks the private journal and binding.
            printf 'Retrying the pending registration from the existing Agent directory.\n'
            if ( unset RIGPILOT_ENROLLMENT_TOKEN RIGPILOT_REPLACEMENT_TOKEN
                 exec "${SOURCE_ROOT}/RigPilotAgent" --config "${CONFIG_FILE}" --once ); then
                enrollment_status=0
            else
                enrollment_status=$?
            fi
            if [[ "${enrollment_status}" -ne 0 && ! -s "${STATE_FILE}" && -t 0 ]] &&
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
            REPLACEMENT_TOKEN="${INSTALL_REPLACEMENT_TOKEN}"
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
            ENROLLMENT_TOKEN="${INSTALL_ENROLLMENT_TOKEN}"
        elif [[ "${replacement_env_set}" -eq 1 ]]; then
            mode="inherit"
            REPLACEMENT_TOKEN="${INSTALL_REPLACEMENT_TOKEN}"
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
             exec "${SOURCE_ROOT}/RigPilotAgent" --config "${CONFIG_FILE}" --once ); then
            enrollment_status=0
        else
            enrollment_status=$?
        fi
    elif [[ "${token_ready}" -eq 1 && "${mode}" == "inherit" ]]; then
        [[ "${REPLACEMENT_TOKEN}" =~ ^rpr_[A-Za-z0-9_-]{43}$ ]] ||
            fail "the inheritance code is invalid; preserve any pending inheritance and retry with its original code"
        if ( unset RIGPILOT_ENROLLMENT_TOKEN
             export RIGPILOT_REPLACEMENT_TOKEN="${REPLACEMENT_TOKEN}"
             exec "${SOURCE_ROOT}/RigPilotAgent" --config "${CONFIG_FILE}" --once ); then
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
            fail "the Controller confirmed registration was not created; use a valid new one-time code when rerunning Install.sh"
        fi
        if [[ "${mode}" == "inherit" ]]; then
            fail "device inheritance could not be confirmed; preserve this Agent directory and retry with the original inheritance code, never a new code"
        fi
        fail "registration could not be confirmed; preserve this Agent directory and retry Install.sh with the original enrollment code"
    fi
    [[ -s "${STATE_FILE}" ]] ||
        fail "identity was not saved; preserve this Agent directory and retry Install.sh with the original code"
}
configure_lan_openssh() {
    # This optional convenience listener is intentionally independent from
    # the Agent.  Only the fixed local account may authenticate, only from private
    # RFC1918/CGNAT addresses, and direct SSH root login remains disabled.
    local account="${RIGPILOT_LAN_SSH_USER:-user}"
    local directory="/etc/ssh/sshd_config.d"
    local target="${directory}/20-rigpilot-lan.conf"
    local stage=""
    local backup=""
    [[ "${account}" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || {
        printf 'WARNING: LAN SSH account name is invalid; skipping OpenSSH setup.\n' >&2
        return 1
    }
    getent passwd "${account}" >/dev/null 2>&1 || {
        printf 'WARNING: Linux account %s does not exist; skipping LAN OpenSSH setup.\n' "${account}" >&2
        return 1
    }
    if ! command -v sshd >/dev/null 2>&1; then
        command -v apt-get >/dev/null 2>&1 || {
            printf 'WARNING: sshd is missing and apt-get is unavailable.\n' >&2
            return 1
        }
        if ! DEBIAN_FRONTEND=noninteractive apt-get update -qq || \
            ! DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server; then
            printf 'WARNING: openssh-server installation failed; Agent controller access is unaffected.\n' >&2
            return 1
        fi
    fi
    install -d -m 0755 -o root -g root "${directory}" || return 1
    if [[ -L "${target}" ]] || [[ -e "${target}" && ! -f "${target}" ]]; then
        printf 'WARNING: unsafe existing LAN SSH drop-in; leaving it unchanged.\n' >&2
        return 1
    fi
    stage=$(mktemp "${directory}/.rigpilot-lan.conf.XXXXXXXXXX") || return 1
    if [[ -f "${target}" ]]; then
        backup=$(mktemp "${directory}/.rigpilot-lan.backup.XXXXXXXXXX") || {
            rm -f -- "${stage}"
            return 1
        }
        cp --preserve=mode,ownership,timestamps -- "${target}" "${backup}" || {
            rm -f -- "${stage}" "${backup}"
            return 1
        }
    fi
    {
        printf '%s\n' "${LAN_SSH_DROPIN_MARKER}"
        printf 'PermitRootLogin no\n'
        printf 'PasswordAuthentication no\n'
        printf 'KbdInteractiveAuthentication no\n'
        printf 'UsePAM yes\n'
        printf 'AllowUsers %s@10.0.0.0/8 %s@172.16.0.0/12 %s@192.168.0.0/16 %s@100.64.0.0/10\n' \
            "${account}" "${account}" "${account}" "${account}"
        printf 'Match User %s Address 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10\n' "${account}"
        printf '    PasswordAuthentication yes\n'
        printf 'Match all\n'
    } >"${stage}"
    chown root:root "${stage}" || { rm -f -- "${stage}" "${backup}"; return 1; }
    chmod 0600 "${stage}" || { rm -f -- "${stage}" "${backup}"; return 1; }
    mv -fT -- "${stage}" "${target}" || {
        rm -f -- "${stage}" "${backup}"
        return 1
    }
    stage=""
    # Capture each effective-policy view before matching.  Piping sshd -T
    # straight into grep made the pipeline lose to a grep early-exit SIGPIPE
    # under pipefail on Ubuntu 26.04 (sshd exits 141 while the match itself
    # succeeded), deterministically failing this convenience setup.
    lan_view="" public_view="" root_view=""
    if ! sshd -t -f /etc/ssh/sshd_config || \
        ! lan_view="$(sshd -T -C "user=${account},addr=192.168.1.2,host=rigpilot")" || \
        ! public_view="$(sshd -T -C "user=${account},addr=203.0.113.2,host=rigpilot")" || \
        ! root_view="$(sshd -T -C "user=root,addr=192.168.1.2,host=rigpilot")" || \
        ! grep -qx 'passwordauthentication yes' <<<"${lan_view}" || \
        ! grep -qx 'passwordauthentication no' <<<"${public_view}" || \
        ! grep -qx 'permitrootlogin no' <<<"${root_view}"; then
        if [[ -n "${backup}" ]]; then
            mv -fT -- "${backup}" "${target}"
            backup=""
        else
            rm -f -- "${target}"
        fi
        printf 'WARNING: LAN SSH policy validation failed; previous sshd configuration was restored.\n' >&2
        return 1
    fi
    if ! systemctl enable ssh.service || ! systemctl reload-or-restart ssh.service || \
        ! systemctl is-active --quiet ssh.service; then
        if [[ -n "${backup}" ]]; then
            mv -fT -- "${backup}" "${target}"
            backup=""
        else
            rm -f -- "${target}"
        fi
        systemctl reload-or-restart ssh.service >/dev/null 2>&1 || true
        printf 'WARNING: OpenSSH could not be started; Agent controller access is unaffected.\n' >&2
        return 1
    fi
    rm -f -- "${backup}"
    printf 'LAN SSH/SFTP enabled for %s on port 22; direct root and public-password SSH are disabled.\n' "${account}"
}

[[ "$(uname -s)" == "Linux" ]] || fail "Linux is required"
case "$(uname -m)" in x86_64|amd64) ;; *) fail "this package supports Ubuntu x86_64 only" ;; esac
[[ "${EUID}" -eq 0 ]] || fail "run with sudo: sudo ./Install.sh"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"
command -v systemctl >/dev/null 2>&1 || fail "systemd is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v flock >/dev/null 2>&1 || fail "flock is required"
case "${RIGPILOT_LAN_SSH:-enable}" in
    enable|preserve) ;;
    *) fail "RIGPILOT_LAN_SSH must be enable or preserve" ;;
esac

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
for asset in RigPilotAgent RigPilotAgentLauncher.sh RigPilotAgentUpdater.py \
    rigpilot-agent.service \
    rigpilot-agent-supervision.conf \
    rigpilot-agent-update-recovery.service rigpilot-agent-update-recovery.timer \
    VERSION SHA256SUMS agent-release.json; do
    [[ -f "${SOURCE_ROOT}/${asset}" && ! -L "${SOURCE_ROOT}/${asset}" ]] || \
        fail "package file is missing or unsafe: ${asset}"
done
# SHA256SUMS must bind every regular package file except itself.  Rejecting
# extras prevents a repacked bundle from smuggling an undeclared executable
# through the otherwise valid checksum list.
python3 - "${SOURCE_ROOT}" <<'PY'
import pathlib
import re
import stat
import sys

root = pathlib.Path(sys.argv[1])
actual = set()
for path in root.rglob("*"):
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise SystemExit("package contains a link or special file")
    if stat.S_ISREG(info.st_mode):
        actual.add(path.relative_to(root).as_posix())
declared = set()
for line in (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"[0-9a-f]{64}  ([^\r\n]+)", line)
    if match is None:
        raise SystemExit("SHA256SUMS has an invalid entry")
    name = match.group(1)
    relative = pathlib.PurePosixPath(name)
    if (relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != name or name in declared):
        raise SystemExit("SHA256SUMS has an invalid entry")
    declared.add(name)
if declared != actual - {"SHA256SUMS"}:
    raise SystemExit("SHA256SUMS does not exactly match the package file set")
PY
(cd -- "${SOURCE_ROOT}" && sha256sum --check --strict SHA256SUMS) || \
    fail "package integrity verification failed"
check_legacy_wifi_dropin


IFS= read -r PACKAGE_VERSION <"${SOURCE_ROOT}/VERSION" || fail "cannot read package version"
[[ "${PACKAGE_VERSION}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || \
    fail "package version is invalid"
[[ "$(wc -l <"${SOURCE_ROOT}/VERSION")" -eq 1 ]] || fail "VERSION is invalid"
if ! SOURCE_VERSION=$("${SOURCE_ROOT}/RigPilotAgent" --version 2>&1); then
    fail "RigPilotAgent failed its version self-check"
fi
[[ "${SOURCE_VERSION}" == "RigPilot Agent ${PACKAGE_VERSION}" ]] || \
    fail "RigPilotAgent does not match VERSION (${SOURCE_VERSION})"

exec 9>/run/lock/rigpilot-agent-install.lock
flock -n 9 || fail "another RigPilot Agent installation is running"
exec 8>/run/lock/rigpilot-agent-update.lock
flock -n 8 || fail "an Agent online update or recovery is running"

for fixed_directory in "${APP_DIR}" "${VERSIONS_DIR}" "${CONFIG_DIR}" "${DATA_DIR}"; do
    if [[ -e "${fixed_directory}" || -L "${fixed_directory}" ]]; then
        [[ -d "${fixed_directory}" && ! -L "${fixed_directory}" ]] || \
            fail "fixed installation directory is unsafe: ${fixed_directory}"
    fi
done
if [[ -e "${SERVICE_DROPIN_DIR}" || -L "${SERVICE_DROPIN_DIR}" ]]; then
    [[ -d "${SERVICE_DROPIN_DIR}" && ! -L "${SERVICE_DROPIN_DIR}" ]] || \
        fail "systemd drop-in directory is unsafe: ${SERVICE_DROPIN_DIR}"
fi
for persistent_file in "${CONFIG_FILE}" "${STATE_FILE}"; do
    if [[ -e "${persistent_file}" || -L "${persistent_file}" ]]; then
        [[ -f "${persistent_file}" && ! -L "${persistent_file}" ]] || \
            fail "existing persistent file is not a regular file: ${persistent_file}"
    fi
done
if [[ -e "${APP_DIR}/RigPilotAgent" || -L "${APP_DIR}/RigPilotAgent" ]]; then
    [[ -f "${APP_DIR}/RigPilotAgent" && ! -L "${APP_DIR}/RigPilotAgent" && \
        "$(stat -c '%u' -- "${APP_DIR}/RigPilotAgent")" == "0" ]] || \
        fail "legacy installed Agent executable is unsafe"
fi

install -d -m 0755 -o root -g root "${APP_DIR}" "${VERSIONS_DIR}" "${CONFIG_DIR}"
install -d -m 0700 -o root -g root \
    "${DATA_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/miner-runtime" \
    "${DATA_DIR}/packages" "${DATA_DIR}/agent-updates"

CONFIG_STAGE=""
SLOT_STAGE=""
SLOT="${VERSIONS_DIR}/${PACKAGE_VERSION}"
SLOT_CREATED=0
early_cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    set +e
    [[ -n "${CONFIG_STAGE}" ]] && rm -f -- "${CONFIG_STAGE}"
    if [[ -n "${SLOT_STAGE}" && -d "${SLOT_STAGE}" && ! -L "${SLOT_STAGE}" ]]; then
        case "${SLOT_STAGE}" in "${VERSIONS_DIR}"/.install-*) rm -rf -- "${SLOT_STAGE}" ;; esac
    fi
    if [[ "${exit_code}" -ne 0 && "${SLOT_CREATED}" -eq 1 && -d "${SLOT}" && ! -L "${SLOT}" ]]; then
        case "${SLOT}" in "${VERSIONS_DIR}"/*) rm -rf -- "${SLOT}" ;; esac
    fi
    [[ "${exit_code}" -eq 0 ]] || warn_registered_agent_offline
    exit "${exit_code}"
}
trap early_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

prepare_existing_identity_config
NEW_CONFIG=0
if [[ ! -e "${CONFIG_FILE}" ]]; then
    NEW_CONFIG=1
    # A pre-supplied code selects unattended first registration. Require its
    # Controller and visible device name explicitly, so a media/bootstrap typo
    # cannot silently claim a device against the default production endpoint.
    if [[ "${INSTALL_ENROLLMENT_TOKEN_SET}" -eq 1 && "${INSTALL_REPLACEMENT_TOKEN_SET}" -eq 1 ]]; then
        fail "enrollment and inheritance codes cannot be supplied together"
    fi
    if [[ "${INSTALL_ENROLLMENT_TOKEN_SET}" -eq 1 || "${INSTALL_REPLACEMENT_TOKEN_SET}" -eq 1 ]]; then
        [[ -n "${RIGPILOT_CONTROLLER_URL:-}" && -n "${RIGPILOT_DEVICE_NAME:-}" ]] ||
            fail "unattended registration requires RIGPILOT_CONTROLLER_URL and RIGPILOT_DEVICE_NAME"
        CONTROLLER_URL="${RIGPILOT_CONTROLLER_URL}"
        DEVICE_NAME="${RIGPILOT_DEVICE_NAME}"
        [[ "${DEVICE_NAME}" =~ [^[:space:]] ]] ||
            fail "RIGPILOT_DEVICE_NAME must contain a visible character"
    elif [[ -t 0 ]]; then
        printf 'Controller URL [example: %s]: ' "${CONTROLLER_DEFAULT}"
        IFS= read -r CONTROLLER_URL
        [[ -n "${CONTROLLER_URL}" && "${CONTROLLER_URL}" != "${CONTROLLER_DEFAULT}" ]] || fail "Enter your Controller HTTPS URL"
        printf 'Device remark/name (optional): '
        IFS= read -r DEVICE_NAME
    else
        fail "new installation needs an interactive terminal or one enrollment/inheritance code with explicit Controller URL and device name"
    fi
    [[ "${CONTROLLER_URL}" =~ ^https://[^[:space:]/]+(:[0-9]+)?(/[^[:space:]]*)?$ ]] || \
        fail "Controller URL must be an HTTPS URL without credentials"
    [[ ${#DEVICE_NAME} -le 80 ]] || fail "device name is too long"
    CONFIG_STAGE="${CONFIG_DIR}/.config.json.install.$$"
    python3 - "${CONFIG_STAGE}" "${CONTROLLER_URL}" "${DEVICE_NAME}" <<'PY'
import json
import os
import sys
from urllib.parse import urlparse

target, controller, name = sys.argv[1:]
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
    "state_file": "/var/lib/rigpilot-agent/state.json",
    "name": name or None,
    "heartbeat_interval": 30,
    "request_timeout": 20,
    "log_level": "INFO",
    "log_file": "/var/lib/rigpilot-agent/logs/agent.jsonl",
    "dry_run": False,
    "service_allowlist": [],
    "allow_reboot": True,
    "allow_shutdown": True,
    "allow_agent_updates": True,
    "agent_update_max_mb": 512,
    "agent_update_total_timeout": 900,
    "collect_gpu_metrics": True,
    "nvidia_smi_path": "nvidia-smi",
    # Advertising an already-listening local sshd is metadata only.  It does
    # not install a daemon, open a port, or change the firewall.
    "advertise_ssh": True,
    "advertise_sftp": True,
    "ssh_port": 22,
    "ssh_username": None,
    "flight_sheet_file": "/var/lib/rigpilot-agent/flight-sheet.json",
    "miner_status_file": "/var/lib/rigpilot-agent/miner-runtime/miner-status.json",
    "managed_mining_enabled": True,
    "managed_miner_runtime_dir": "/var/lib/rigpilot-agent/miner-runtime",
    "miner_profile_allowlist": [],
    "flight_sheet_extra_key_allowlist": [],
    "miner_service": None,
    "allow_miner_downloads": True,
    "miner_packages_dir": "/var/lib/rigpilot-agent/packages",
    "miner_download_host_allowlist": [],
    "miner_download_max_mb": 512,
    "miner_download_total_timeout": 300,
    "allow_gpu_oc": True,
    "gpu_oc_allowed_vendors": ["nvidia"],
    "command_history_limit": 200,
}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    chown root:root "${CONFIG_STAGE}"
else
    # Existing identity, policy and controller settings are preserved.  This
    # one deliberate migration opts a systemd installation into the fixed
    # protocol-v1 updater installed by this package; retired feature keys are
    # dropped so the staged configuration stays minimal.
    CONFIG_STAGE="${CONFIG_DIR}/.config.json.install.$$"
    python3 - "${CONFIG_FILE}" "${CONFIG_STAGE}" <<'PY'
import json
import os
import sys

source, target = sys.argv[1:]
with open(source, "r", encoding="utf-8-sig") as handle:
    value = json.load(handle)
if not isinstance(value, dict):
    raise SystemExit("configuration root must be an object")
value["allow_agent_updates"] = True
value.pop("allow_wifi_management", None)
value.pop("allow_nvidia_driver_updates", None)
value.pop("allow_remote_sessions", None)
value.pop("allow_remote_root_sessions", None)
value.pop("allow_local_web", None)
value.pop("local_web_port", None)
value.pop("allow_hiveos_runtime_preparation", None)
value.pop("hiveos_max_archive_bytes", None)
value.pop("hiveos_max_expanded_bytes", None)
value.pop("hiveos_min_free_bytes", None)
value.setdefault("agent_update_max_mb", 512)
value.setdefault("agent_update_total_timeout", 900)
value["advertise_ssh"] = True
value["advertise_sftp"] = True
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(value, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    chown root:root "${CONFIG_STAGE}"
fi

if [[ "${NEW_CONFIG}" -eq 1 ]]; then
    install -m 0600 -o root -g root "${CONFIG_STAGE}" "${CONFIG_FILE}"
fi
if [[ ! -s "${STATE_FILE}" ]]; then
    enroll_agent_identity
fi
unset INSTALL_ENROLLMENT_TOKEN INSTALL_REPLACEMENT_TOKEN
chmod 0600 "${CONFIG_FILE}" "${STATE_FILE}"
chown root:root "${CONFIG_FILE}" "${STATE_FILE}"

if [[ -e "${SLOT}" || -L "${SLOT}" ]]; then
    [[ -d "${SLOT}" && ! -L "${SLOT}" && -f "${SLOT}/RigPilotAgent" && \
        ! -L "${SLOT}/RigPilotAgent" ]] || fail "existing version slot is unsafe"
    [[ "$(sha256sum "${SLOT}/RigPilotAgent" | awk '{print $1}')" == \
        "$(sha256sum "${SOURCE_ROOT}/RigPilotAgent" | awk '{print $1}')" ]] || \
        fail "version ${PACKAGE_VERSION} is already installed with different bytes"
else
    SLOT_STAGE="${VERSIONS_DIR}/.install-${PACKAGE_VERSION}-$$"
    [[ ! -e "${SLOT_STAGE}" && ! -L "${SLOT_STAGE}" ]] || fail "slot staging path exists"
    install -d -m 0700 -o root -g root "${SLOT_STAGE}"
    install -m 0755 -o root -g root "${SOURCE_ROOT}/RigPilotAgent" "${SLOT_STAGE}/RigPilotAgent"
    [[ "$("${SLOT_STAGE}/RigPilotAgent" --version)" == "RigPilot Agent ${PACKAGE_VERSION}" ]] || \
        fail "installed version slot failed its self-check"
    chmod 0755 "${SLOT_STAGE}"
    SLOT_CREATED=1
    mv -T -- "${SLOT_STAGE}" "${SLOT}"
    SLOT_STAGE=""
fi

ROLLBACK_DIR=$(mktemp -d "${APP_DIR}/.install-rollback.XXXXXXXXXX")
chmod 0700 "${ROLLBACK_DIR}"
SUPERVISION_STAGE=""
SERVICE_WAS_ACTIVE=0
SERVICE_WAS_ENABLED=0
TIMER_WAS_ENABLED=0
TIMER_WAS_ACTIVE=0
CHANGES_STARTED=0
COMMITTED=0

backup_file() {
    local source="$1" name="$2"
    if [[ -f "${source}" && ! -L "${source}" ]]; then
        install -m 0600 -o root -g root "${source}" "${ROLLBACK_DIR}/${name}"
        printf '1' >"${ROLLBACK_DIR}/${name}.present"
    else
        printf '0' >"${ROLLBACK_DIR}/${name}.present"
    fi
}
restore_file() {
    local destination="$1" name="$2" mode="$3"
    local present=""
    if ! present=$(cat -- "${ROLLBACK_DIR}/${name}.present"); then
        printf 'Cannot read rollback presence marker for %s; leaving the current file unchanged.\n' "${destination}" >&2
        return 1
    fi
    if [[ "${present}" == "1" ]]; then
        install -m "${mode}" -o root -g root "${ROLLBACK_DIR}/${name}" "${destination}"
    elif [[ "${present}" == "0" ]]; then
        rm -f -- "${destination}"
    else
        printf 'Invalid rollback presence marker for %s; leaving the current file unchanged.\n' "${destination}" >&2
        return 1
    fi
}
for spec in \
    "${APP_DIR}/RigPilotAgentLauncher.sh:launcher" \
    "${APP_DIR}/Setup-HiveOS-Runtime.sh:hiveos-setup" \
    "${APP_DIR}/RigPilotAgentUpdater.py:updater" \
    "${ACTIVE_VERSION_FILE}:active-version" \
    "${SERVICE_FILE}:service" \
    "${SUPERVISION_DROPIN}:supervision-dropin" \
    "${REMOTE_SESSION_DROPIN}:remote-dropin" \
    "${LEGACY_WIFI_DROPIN}:legacy-wifi-dropin" \
    "${RECOVERY_SERVICE}:recovery-service" \
    "${RECOVERY_TIMER}:recovery-timer" \
    "${CONFIG_FILE}:config"; do
    backup_file "${spec%%:*}" "${spec##*:}"
done
systemctl is-active --quiet rigpilot-agent.service 2>/dev/null && SERVICE_WAS_ACTIVE=1 || true
systemctl is-enabled --quiet rigpilot-agent.service 2>/dev/null && SERVICE_WAS_ENABLED=1 || true
systemctl is-enabled --quiet rigpilot-agent-update-recovery.timer 2>/dev/null && TIMER_WAS_ENABLED=1 || true
systemctl is-active --quiet rigpilot-agent-update-recovery.timer 2>/dev/null && TIMER_WAS_ACTIVE=1 || true

finish_install() {
    local exit_code=$?
    local rollback_incomplete=0
    trap - EXIT INT TERM
    set +e
    if [[ "${exit_code}" -ne 0 && "${CHANGES_STARTED}" -eq 1 && "${COMMITTED}" -eq 0 ]]; then
        printf 'Installation failed after activation began; restoring the previous Agent service.\n' >&2
        if ! systemctl stop rigpilot-agent.service >/dev/null 2>&1; then
            local stopped_state=""
            stopped_state=$(systemctl show --property=ActiveState --value rigpilot-agent.service 2>/dev/null) || stopped_state=""
            case "${stopped_state}" in
                inactive|failed) ;;
                *) rollback_incomplete=1
                   printf 'Could not confirm the Agent service stopped; no rollback files were restored.\n' >&2 ;;
            esac
        fi
        if [[ "${rollback_incomplete}" -eq 0 ]]; then
            restore_file "${APP_DIR}/RigPilotAgentLauncher.sh" launcher 0755 || rollback_incomplete=1
            restore_file "${APP_DIR}/Setup-HiveOS-Runtime.sh" hiveos-setup 0755 || rollback_incomplete=1
            restore_file "${APP_DIR}/RigPilotAgentUpdater.py" updater 0755 || rollback_incomplete=1
            restore_file "${ACTIVE_VERSION_FILE}" active-version 0644 || rollback_incomplete=1
            restore_file "${SERVICE_FILE}" service 0644 || rollback_incomplete=1
            install -d -m 0755 -o root -g root "${SERVICE_DROPIN_DIR}" || rollback_incomplete=1
            restore_file "${SUPERVISION_DROPIN}" supervision-dropin 0644 || rollback_incomplete=1
            restore_file "${REMOTE_SESSION_DROPIN}" remote-dropin 0644 || rollback_incomplete=1
            restore_file "${LEGACY_WIFI_DROPIN}" legacy-wifi-dropin 0644 || rollback_incomplete=1
            restore_file "${RECOVERY_SERVICE}" recovery-service 0644 || rollback_incomplete=1
            restore_file "${RECOVERY_TIMER}" recovery-timer 0644 || rollback_incomplete=1
            restore_file "${CONFIG_FILE}" config 0600 || rollback_incomplete=1
            systemctl daemon-reload >/dev/null 2>&1 || rollback_incomplete=1
        fi
        # Explicit branches: the previous ``cond && enable || disable`` chains
        # ran the disable leg whenever enable itself failed, inverting the
        # intended rollback state.  Each leg stays best-effort.
        # Do not restart a partially restored launcher/configuration.  Keep
        # every backup and the new slot if any restoration step fails: the
        # active pointer may still refer to that slot and recovery needs both.
        if [[ "${rollback_incomplete}" -eq 0 ]]; then
            if [[ "${SERVICE_WAS_ENABLED}" -eq 1 ]]; then
                systemctl enable rigpilot-agent.service >/dev/null 2>&1 || rollback_incomplete=1
            else
                systemctl disable rigpilot-agent.service >/dev/null 2>&1 || rollback_incomplete=1
            fi
            if [[ "${TIMER_WAS_ENABLED}" -eq 1 ]]; then
                systemctl enable rigpilot-agent-update-recovery.timer >/dev/null 2>&1 || rollback_incomplete=1
            else
                systemctl disable rigpilot-agent-update-recovery.timer >/dev/null 2>&1 || rollback_incomplete=1
            fi
            if [[ "${SERVICE_WAS_ACTIVE}" -eq 1 ]]; then
                systemctl start rigpilot-agent.service >/dev/null 2>&1 || rollback_incomplete=1
            fi
            if [[ "${TIMER_WAS_ACTIVE}" -eq 1 ]]; then
                systemctl start rigpilot-agent-update-recovery.timer >/dev/null 2>&1 || rollback_incomplete=1
            else
                systemctl stop rigpilot-agent-update-recovery.timer >/dev/null 2>&1 || rollback_incomplete=1
            fi
        fi
        if [[ "${rollback_incomplete}" -eq 0 ]]; then
            printf 'Existing configuration and device identity were preserved.\n' >&2
        else
            printf 'WARNING: automatic rollback was incomplete; previous files remain in %s.\n' "${ROLLBACK_DIR}" >&2
            printf 'The new version slot was retained. Resolve the restoration error before retrying installation.\n' >&2
        fi
    fi
    [[ "${exit_code}" -eq 0 ]] || warn_registered_agent_offline
    [[ -n "${CONFIG_STAGE:-}" ]] && rm -f -- "${CONFIG_STAGE}"
    [[ -n "${SUPERVISION_STAGE:-}" ]] && rm -f -- "${SUPERVISION_STAGE}"
    if [[ "${exit_code}" -ne 0 && "${COMMITTED}" -eq 0 && "${rollback_incomplete}" -eq 0 && "${SLOT_CREATED}" -eq 1 &&
          -d "${SLOT}" && ! -L "${SLOT}" ]]; then
        rm -rf -- "${SLOT}"
    fi
    if [[ "${rollback_incomplete}" -eq 0 && -d "${ROLLBACK_DIR}" && ! -L "${ROLLBACK_DIR}" ]]; then
        case "${ROLLBACK_DIR}" in "${APP_DIR}"/.install-rollback.*) rm -rf -- "${ROLLBACK_DIR}" ;; esac
    fi
    exit "${exit_code}"
}
trap finish_install EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

CHANGES_STARTED=1
systemctl stop rigpilot-agent.service >/dev/null 2>&1 || true
systemctl stop rigpilot-agent-update-recovery.timer >/dev/null 2>&1 || true
install -m 0755 -o root -g root "${SOURCE_ROOT}/RigPilotAgentLauncher.sh" \
    "${APP_DIR}/RigPilotAgentLauncher.sh"
install -m 0755 -o root -g root "${SOURCE_ROOT}/RigPilotAgentUpdater.py" \
    "${APP_DIR}/RigPilotAgentUpdater.py"
printf '%s\n' "${PACKAGE_VERSION}" >"${APP_DIR}/.active-version.install.$$"
chown root:root "${APP_DIR}/.active-version.install.$$"
chmod 0644 "${APP_DIR}/.active-version.install.$$"
mv -fT -- "${APP_DIR}/.active-version.install.$$" "${ACTIVE_VERSION_FILE}"
rm -f -- "${APP_DIR}/pending-update.json" "${APP_DIR}/updater-state.json"
install -m 0600 -o root -g root "${CONFIG_STAGE}" "${CONFIG_FILE}"
install -m 0644 -o root -g root "${SOURCE_ROOT}/rigpilot-agent.service" "${SERVICE_FILE}"
install -m 0644 -o root -g root "${SOURCE_ROOT}/rigpilot-agent-update-recovery.service" \
    "${RECOVERY_SERVICE}"
install -m 0644 -o root -g root "${SOURCE_ROOT}/rigpilot-agent-update-recovery.timer" \
    "${RECOVERY_TIMER}"
install -d -m 0755 -o root -g root "${SERVICE_DROPIN_DIR}"
SUPERVISION_STAGE="${SERVICE_DROPIN_DIR}/.supervision.conf.install.$$"
install -m 0644 -o root -g root "${SOURCE_ROOT}/rigpilot-agent-supervision.conf" \
    "${SUPERVISION_STAGE}"
mv -fT -- "${SUPERVISION_STAGE}" "${SUPERVISION_DROPIN}"
SUPERVISION_STAGE=""
# Successful upgrades remove the retired Wi-Fi and Spark remote-session
# permissions before the new unit starts.  The rollback snapshots restore
# them if activation fails.
rm -f -- "${LEGACY_WIFI_DROPIN}"
rm -f -- "${REMOTE_SESSION_DROPIN}"
systemctl daemon-reload
systemctl enable rigpilot-agent.service rigpilot-agent-update-recovery.timer
systemctl restart rigpilot-agent.service
systemctl start rigpilot-agent-update-recovery.timer
systemctl is-active --quiet rigpilot-agent.service || \
    fail "systemd installed the service but it did not stay active"
systemctl is-active --quiet rigpilot-agent-update-recovery.timer || \
    fail "online update recovery timer did not stay active"
COMMITTED=1
# Older manual packages used these non-slotted paths.  Remove them only after
# the launcher-backed slot is active; they are never touched during rollback.
rm -f -- "${APP_DIR}/Setup-HiveOS-Runtime.sh"
rm -f -- "${APP_DIR}/RigPilotAgent" || \
    printf 'WARNING: the unused legacy Agent executable could not be removed.\n' >&2
# A manual installation is an operator-confirmed boundary: reclaim the staging
# and quarantine slots the fixed updater preserved from concluded online
# updates.  A rollback intentionally keeps the failed slot as evidence; the
# next online update or this reinstall reclaims it, so repeated failures
# cannot accumulate roughly twice the package size per attempt.
while IFS= read -r -d '' abandoned_slot; do
    abandoned_name="${abandoned_slot##*/}"
    [[ "${abandoned_name}" =~ ^\.(incoming|rolled-back)-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{32}$ ]] || \
        continue
    rm -rf --one-file-system -- "${abandoned_slot}" 2>/dev/null || true
done < <(find "${VERSIONS_DIR}" -maxdepth 1 -mindepth 1 -name '.*' -type d -print0 2>/dev/null)

# Optional host SSH setup is deliberately post-commit: package/network failure
# must never roll back an otherwise healthy Agent installation.  The Agent
# remains connected to its controller regardless of this result.  Appliance images
# set this policy to "preserve" because their local browser Shell uses HTTPS
# and first-boot enrollment must not block on an apt repository.
case "${RIGPILOT_LAN_SSH:-enable}" in
    enable) configure_lan_openssh || true ;;
    preserve) ;;
esac

if [[ "${NEW_CONFIG}" -eq 1 ]]; then
    printf 'RigPilot Agent registered and installed successfully.\n'
else
    printf 'RigPilot Agent updated successfully; identity, configuration and data were preserved.\n'
fi
printf 'Online Agent updates are enabled through the fixed protocol-v1 updater.\n'
printf 'Status: systemctl status rigpilot-agent --no-pager\n'
