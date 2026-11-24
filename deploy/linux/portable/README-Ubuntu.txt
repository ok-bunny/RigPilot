RigPilot Agent %%VERSION%% for Ubuntu x86_64
=======================================

Supported target: Ubuntu 22.04 / 24.04 / 26.04 x86_64 with systemd.

Recommended system installation
--------------------------------
1. In the controller, generate a one-time enrollment code for a new device, or
   an inheritance code for the specific existing device you are replacing.
   Stop the old Agent and wait until that device is offline before inheritance.
2. Extract this archive on the Ubuntu device.
3. Run: chmod +x *.sh RigPilotAgent
4. Run: sudo ./Install.sh
5. Enter the controller URL and optional device remark. With no saved identity,
   choose 1 (new device, default) or 2 (inherit an existing device), then enter
   the matching code at the hidden prompt. A saved identity is reused without
   a registration choice.

The controller must already support the current enrollment recovery protocol.
An already-enrolled device keeps its identity and does not need to enroll again.
For unattended first installation without config.json, supply exactly one of
RIGPILOT_ENROLLMENT_TOKEN or RIGPILOT_REPLACEMENT_TOKEN in the protected Agent
process environment, together with nonempty RIGPILOT_CONTROLLER_URL and
RIGPILOT_DEVICE_NAME. The installer requires an explicit HTTPS Controller URL
and visible name in this path; it will not choose the production default or
wait for a prompt. An existing config.json is reused. Never place either code
in command arguments, shell history, config.json, or logs.

If registration is interrupted or its result cannot be saved, keep the same
configuration and data directory and run Install.sh again. Ordinary enrollment
first retries its private pending attempt without a code; if the first request
never reached the controller, give only that attempt's original enrollment code.
Pending inheritance requires its original inheritance code on every retry. Do
not switch modes, generate a new code, delete a pending journal or move to a new
data directory after an uncertain result. Two simultaneous pending journals are
rejected for manual resolution. A confirmed ordinary enrollment rejection that
clears its journal permits a valid new enrollment code on the next run.
Inheritance is limited to the device selected in the Controller and may not
claim an online old Agent. After inheriting across operating systems or GPU
layouts, review that device's flight sheet and host-specific settings before
starting mining.

The installer keeps root-owned launcher/updater trust anchors and versioned
program slots in /opt/rigpilot-agent, configuration in
/etc/rigpilot-agent, and identity, logs, miner packages, active flight sheet,
and runtime data in /var/lib/rigpilot-agent. Re-running Install.sh upgrades the
program while preserving configuration and device identity.

After this one manual installation, agent_update.v1 is enabled. The controller
can send the matching Ubuntu OnlineUpdate archive. The fixed installed updater
checks its archive, installs only RigPilotAgent into a new version slot, waits
for two authenticated heartbeats, and rolls back if the replacement does not
reconnect. A recovery timer completes or rolls back an update interrupted by a
reboot. Online packages cannot replace RigPilotAgentLauncher.sh or
RigPilotAgentUpdater.py; changing those trust anchors requires a deliberate
manual Install.sh run with a complete package.

The system installation also adds a fixed supervision.conf systemd drop-in.
The Agent notifies systemd only when its main heartbeat loop continues making
progress. If that progress stops for 120 seconds, systemd restarts the Agent
process. The base unit deliberately keeps KillMode=process, so this recovery
does not stop or restart a miner running in its separate POSIX session. Agent
update activation remains coordinated by the fixed updater and service restart
policy; the watchdog does not introduce a separate timer or updater process.

Before replacing an installed binary, Install.sh verifies the staged program's
reported version and saves the previous binary and systemd unit. If service
reload, enable, restart, or active-state verification fails, it restores the
previous files and service state automatically. On a failed first install, the
new program/unit are removed while any configuration and enrolled identity are
left in place for diagnosis or retry.
If restoring the previous files or service fails, installation reports an
incomplete rollback and preserves the root-only rollback directory plus the
new version slot. Resolve that error before retrying; do not delete those
recovery files based only on their temporary-looking names.

If installation fails after saving the device identity and no Agent service is
running, the installer prints a temporary foreground command using the original
/etc/rigpilot-agent/config.json. Keep that terminal open until the service is
repaired, then rerun Install.sh to restore background startup. Do not use
Start-Portable.sh to recover this installed identity: it uses a separate data
directory and may request another enrollment code instead.

Portable foreground mode
------------------------
Run sudo ./Start-Portable.sh. Its data stays in the extracted data directory.
With no saved identity, enter the Controller URL and optional device remark,
then choose 1 (new device, default) or 2 (inherit an existing device). Enter the
matching code at the hidden prompt. A saved identity is reused without a new
choice or code. This entry uses the extracted data directory; Install.sh uses
the separate protected system installation paths described above.

Unattended first contact requires exactly one of RIGPILOT_ENROLLMENT_TOKEN or
RIGPILOT_REPLACEMENT_TOKEN. When configuration is absent, also provide an
explicit HTTPS RIGPILOT_CONTROLLER_URL and visible RIGPILOT_DEVICE_NAME.
Codes reach only the selected Agent --once request, not configuration helpers
or the continuing foreground Agent. Do not put them in shell history or argv.

After an uncertain result, preserve this same data directory and rerun
Start-Portable.sh. Pending registration first attempts recovery without a code;
an unsubmitted attempt can retry only its original enrollment code. Pending
inheritance requires its original inheritance code. Do not switch modes or
replace pending files. A saved identity or pending claim without its existing
configuration is refused rather than creating a new configuration for it.

Press Ctrl+C to stop the Agent. A managed miner runs in its own POSIX session;
stopping the Agent does not intentionally stop that verified miner.
Portable foreground mode intentionally keeps allow_agent_updates=false because
it has no fixed root-owned launcher, updater, version slots, or recovery timer.

Structured remote access
------------------------
The local web terminal/file manager, Spark remote sessions and their
configuration switches were removed in Agent 0.8.58, together with the HiveOS
runtime environment and NVIDIA driver update management. A successful
Install.sh upgrade removes the retired remote-sessions.conf systemd drop-in
and the retired Setup-HiveOS-Runtime.sh wrapper, restoring the hardened base
service sandbox. Old configuration keys from those features are ignored.

The system installer also prepares LAN-only OpenSSH for the fixed `user`
account (override locally with RIGPILOT_LAN_SSH_USER). If sshd is absent on an
Ubuntu host it tries to install openssh-server, validates the complete sshd
configuration with `sshd -t`/`sshd -T`, then enables port 22. Password login is
allowed only from RFC1918 or CGNAT source addresses. Direct SSH root login and
public-source password login remain disabled. A package/repository/sshd failure
is reported as a warning after the Agent transaction has committed; the Agent
stays connected to its controller regardless of this result.

Useful commands
---------------
systemctl status rigpilot-agent --no-pager
journalctl -u rigpilot-agent -g watchdog -n 50 --no-pager
journalctl -u rigpilot-agent -n 100 --no-pager
sudo systemctl restart rigpilot-agent
sudo ./Remove-Systemd.sh

Remove-Systemd.sh preserves identity/data unless --purge-data is explicitly
supplied.

PCI ID Project data license
--------------------------
The embedded database notice is also available offline with --third-party-notices.

PCI ID Project data, derived from pciutils/pciids pci.ids
Copyright Martin Mares and Albert Pool.

The upstream database and generated files offer GPL-2.0-or-later or
BSD-3-Clause. RigPilot distributes this derived table under BSD-3-Clause.
Upstream permission: https://github.com/pciutils/pciids/blob/3452638d16a34aa3d6e427d21e4cd0bae0fb884e/README.md
Standard license: https://opensource.org/license/bsd-3-clause

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright
   notice, this list of conditions and the following disclaimer in the
   documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.

Seine CPU mining
----------------
Seine supports BNT (blocknet-argon2id) CPU pool jobs. Omitted threads are
automatic; profiles are balanced, throughput and efficiency. The pool
protocol does not accept a password. Shares remain unknown when the miner
API does not report them. Official Seine 0.2.15 Linux assets require glibc
2.39. On Ubuntu 22.04, explicitly select the separately distributed RigPilot
glibc 2.35 CPU compatibility build as an uploaded package; its own archive
SHA identifies that build and never replaces the pinned official asset.
