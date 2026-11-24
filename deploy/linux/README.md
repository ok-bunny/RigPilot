# Ubuntu Agent packaging

`build-ubuntu-portable.sh` builds a standalone Ubuntu x86_64 Agent archive on
an x86_64 Linux builder. It runs the Agent tests, freezes a single executable
with PyInstaller, verifies the embedded version, writes per-file hashes, and
creates a complete manual package plus a minimal controller payload:

- `outputs/RigPilotAgent-X.Y.Z-Ubuntu-x86_64.tar.gz`
- `outputs/RigPilotAgent-X.Y.Z-Ubuntu-x86_64-OnlineUpdate.tar.gz`

Archive and SHA-256 sidecar publication uses same-directory hard links and
refuses existing output names. The output filesystem must support hard links.
If a sidecar fails after its archive was published, the archive remains and the
build reports failure; only private staging files are cleaned up. Both files
must be present for a complete delivery.

```bash
bash deploy/linux/build-ubuntu-portable.sh
```

To also build and verify the Controller archive, run
`bash deploy/build-all-releases.sh` in this Linux build environment. This entry
point builds Linux-local artifacts only; it does not require `powershell.exe`
or a cross-OS path converter. Build Windows archives independently in the
Windows VM, then collect the artifacts in the shared output directory.

The build host determines glibc compatibility. Release builds are deliberately
gated to Ubuntu 22.04 x86_64 with glibc 2.35 or older, so the result also runs
on Ubuntu 24.04. The script rejects Ubuntu 24.04 and any glibc newer than 2.35;
do not bypass that check for a public package. A builder can be checked without
installing dependencies or compiling anything:

```bash
bash deploy/linux/build-ubuntu-portable.sh --check-build-host
```

The archive supports foreground portable use and a fixed systemd installation.
The installer preserves `/etc/rigpilot-agent` and `/var/lib/rigpilot-agent` on
an upgrade. One manual systemd installation enables `agent_update.v1` through
root-owned fixed launcher/updater files, version slots and a recovery timer.
The online payload contains only the Agent binary and release manifest;
protocol v1 never replaces its installed launcher or updater. Portable
foreground mode continues to advertise no online-update capability.

Agent 0.6.15 起会通过 psutil 读取 Linux 内核 hwmon 温度，优先选择 AMD
`k10temp` 的 Tctl/Tdie 与 Intel `coretemp` 的 Package/Physical ID 传感器。只有
明确属于 CPU 且处于合理范围的读数才会上报；内核模块、硬件或权限不提供读数时，
控制台显示“温度未上报”，不会影响心跳、在线更新或挖矿进程。

The base systemd unit uses `ProtectHome=true`, `ProtectSystem=strict`, and
`NoNewPrivileges=true`. HiveOS runtime preparation, Spark remote sessions,
the LAN web terminal and NVIDIA driver updates were removed in Agent 0.8.58;
the installer now removes the retired `remote-sessions.conf` drop-in and the
retired `Setup-HiveOS-Runtime.sh` wrapper during a successful upgrade so the
hardened service sandbox applies again. OnlineUpdate remains exactly the Agent binary and release
manifest; neither downloading nor activating an online Agent update runs APT
or changes the installed launcher/updater.
