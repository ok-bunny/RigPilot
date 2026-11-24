#!/bin/sh
# Restore executable bits to the Linux/macOS source files used by RigPilot.
set -eu

script_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)
cd "$script_dir/.."

set -- \
    deploy/appliance/apply-final-image-policy.py \
    deploy/appliance/build-rigpilotos-target.sh \
    deploy/appliance/memory/collect-memory.py \
    deploy/appliance/memory/measure-image.py \
    deploy/appliance/rootfs-overlay/etc/grub.d/41_rigpilotos_pci_recovery \
    deploy/appliance/rootfs-overlay/etc/profile.d/rigpilotos-welcome.sh \
    deploy/appliance/rootfs-overlay/usr/libexec/rigpilot-firstboot \
    deploy/appliance/rootfs-overlay/usr/libexec/rigpilotos-grow-root \
    deploy/appliance/rootfs-overlay/usr/libexec/rigpilotos-nvidia-guard \
    deploy/appliance/rootfs-overlay/usr/libexec/rigpilotos-pci-bar \
    deploy/appliance/rootfs-overlay/usr/libexec/rigpilotos-smoke-ready \
    deploy/appliance/rootfs-overlay/usr/local/sbin/rigpilot-control \
    deploy/appliance/smoke-console-login.py \
    deploy/appliance/verify-enrolled-console.py \
    deploy/appliance/verify-unregistered-console.py \
    deploy/build-all-releases.sh \
    deploy/deploy-controller-release.sh \
    deploy/linux/build-ubuntu-portable.sh \
    deploy/linux/portable/Install.sh \
    deploy/linux/portable/Remove-Systemd.sh \
    deploy/linux/portable/RigPilotAgentLauncher.sh \
    deploy/linux/portable/RigPilotAgentUpdater.py \
    deploy/linux/portable/Start-Portable.sh \
    deploy/smoke-controller-release.py \
    deploy/smoke-controller-target.py \
    deploy/windows/smoke-inheritance-retry.py \
    scripts/run-server-mac.sh \
    scripts/run-server.sh \
    scripts/setup-mac.sh

# Validate the complete list before making changes.
for file do
    if [ ! -f "$file" ]; then
        printf 'Expected a regular file: %s\n' "$file" >&2
        exit 1
    fi
    entry=$file
    while [ "$entry" != "${entry%/*}" ]; do
        if [ -L "$entry" ]; then
            printf 'Refusing a symlink: %s\n' "$entry" >&2
            exit 1
        fi
        entry=${entry%/*}
    done
    if [ -L "$entry" ]; then
        printf 'Refusing a symlink: %s\n' "$entry" >&2
        exit 1
    fi
done

chmod a+x "$@"
