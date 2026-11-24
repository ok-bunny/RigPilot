#!/usr/bin/env bash
set -Eeuo pipefail

readonly LAN_SSH_DROPIN="/etc/ssh/sshd_config.d/20-rigpilot-lan.conf"
# Identical to Install.sh's LAN_SSH_DROPIN_MARKER (tests assert the constants
# stay aligned).  The agent-side configure_linux_ssh.v1 writer and Install.sh
# releases before 0.8.58 wrote the earlier Spark wording, so both RigPilot-
# authored first lines are removed; any other content stays untouched.
readonly LAN_SSH_MARKER="# Managed by RigPilot: password SSH is LAN-only; the Agent does not use port 22."
readonly LAN_SSH_LEGACY_MARKER="# Managed by RigPilot: password SSH is LAN-only; Spark does not use port 22."

remove_managed_lan_ssh_dropin() {
    local first_line=""
    local backup=""
    [[ -e "${LAN_SSH_DROPIN}" || -L "${LAN_SSH_DROPIN}" ]] || return 0
    if [[ -L "${LAN_SSH_DROPIN}" || ! -f "${LAN_SSH_DROPIN}" ]]; then
        echo "Refusing to remove unsafe RigPilot SSH drop-in: ${LAN_SSH_DROPIN}" >&2
        return 1
    fi
    IFS= read -r first_line <"${LAN_SSH_DROPIN}" || true
    if [[ "${first_line}" != "${LAN_SSH_MARKER}" && "${first_line}" != "${LAN_SSH_LEGACY_MARKER}" ]]; then
        echo "Leaving non-RigPilot SSH drop-in unchanged: ${LAN_SSH_DROPIN}" >&2
        return 0
    fi
    command -v sshd >/dev/null 2>&1 || {
        echo "Cannot validate SSH after removal because sshd is unavailable." >&2
        return 1
    }
    backup=$(mktemp "/etc/ssh/sshd_config.d/.rigpilot-remove.XXXXXXXXXX")
    cp --preserve=mode,ownership,timestamps -- "${LAN_SSH_DROPIN}" "${backup}"
    rm -f -- "${LAN_SSH_DROPIN}"
    if ! sshd -t -f /etc/ssh/sshd_config; then
        mv -fT -- "${backup}" "${LAN_SSH_DROPIN}"
        echo "SSH validation failed; the RigPilot drop-in was restored." >&2
        return 1
    fi
    if ! systemctl reload-or-restart ssh.service; then
        mv -fT -- "${backup}" "${LAN_SSH_DROPIN}"
        sshd -t -f /etc/ssh/sshd_config >/dev/null 2>&1 && \
            systemctl reload-or-restart ssh.service >/dev/null 2>&1 || true
        echo "SSH reload failed; the RigPilot drop-in was restored." >&2
        return 1
    fi
    rm -f -- "${backup}"
    echo "Removed the RigPilot-managed LAN SSH policy; OpenSSH itself remains installed."
}

[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
command -v flock >/dev/null 2>&1 || { echo "flock is required." >&2; exit 1; }
exec 9>/run/lock/rigpilot-agent-install.lock
flock -n 9 || { echo "An Agent installation is running; removal was cancelled." >&2; exit 1; }
exec 8>/run/lock/rigpilot-agent-update.lock
flock -n 8 || { echo "An Agent online update is running; removal was cancelled." >&2; exit 1; }
remove_managed_lan_ssh_dropin
systemctl disable --now rigpilot-agent.service 2>/dev/null || true
systemctl disable --now rigpilot-agent-update-recovery.timer 2>/dev/null || true
rm -f -- /etc/systemd/system/rigpilot-agent.service.d/supervision.conf
rm -f -- /etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf
rm -f -- /etc/systemd/system/rigpilot-agent.service.d/wifi-management.conf
rmdir -- /etc/systemd/system/rigpilot-agent.service.d 2>/dev/null || true
rm -f -- /etc/systemd/system/rigpilot-agent.service
rm -f -- /etc/systemd/system/rigpilot-agent-update-recovery.service
rm -f -- /etc/systemd/system/rigpilot-agent-update-recovery.timer
if [[ -d /opt/rigpilot-agent && ! -L /opt/rigpilot-agent && \
    "$(realpath -e -- /opt/rigpilot-agent)" == "/opt/rigpilot-agent" ]]; then
    rm -rf --one-file-system -- /opt/rigpilot-agent
elif [[ -e /opt/rigpilot-agent || -L /opt/rigpilot-agent ]]; then
    echo "Refusing to remove unsafe /opt/rigpilot-agent." >&2
    exit 1
fi
systemctl daemon-reload

if [[ "${1:-}" == "--purge-data" ]]; then
    for persistent in /etc/rigpilot-agent /var/lib/rigpilot-agent; do
        if [[ -d "${persistent}" && ! -L "${persistent}" && \
            "$(realpath -e -- "${persistent}")" == "${persistent}" ]]; then
            rm -rf --one-file-system -- "${persistent}"
        elif [[ -e "${persistent}" || -L "${persistent}" ]]; then
            echo "Refusing to purge unsafe path: ${persistent}" >&2
            exit 1
        fi
    done
    echo "RigPilot Agent and its identity/data were removed."
else
    echo "RigPilot Agent was removed. Identity/configuration remain in /etc and /var/lib."
    echo "Use sudo ./Remove-Systemd.sh --purge-data only when permanent deletion is intended."
fi
