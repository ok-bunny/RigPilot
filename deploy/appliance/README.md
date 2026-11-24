# RigPilotOS image recipe

This directory contains the Linux disk builder, its Ubuntu 24.04 amd64 filesystem overlay, first-boot enrollment, the NVIDIA runtime checks, and the console and memory verification tools. The image recipe identifies itself as 0.2.12. Build inputs are supplied separately.

## Inputs and build

From the repository root, run `sh scripts/prepare-unix-modes.sh` before building. The image overlay includes executable boot hooks and services.

1. Build a [Linux Agent full archive](../linux/README.md). The online-update archive lacks the installer required for first boot.
2. Obtain `NVIDIA-Linux-x86_64-615.78.08.run` from NVIDIA and the three matching Ubuntu packages listed below from a trusted Ubuntu package archive. Put the `.deb` files under `deploy/appliance/vendor/`. The builder verifies the expected hashes before touching the target disk.

   - `dkms_3.4.3-1ubuntu1_all.deb`
   - `linux-image-generic_6.8.0-142.142_amd64.deb`
   - `linux-headers-generic_6.8.0-142.142_amd64.deb`

3. Use a Linux build machine with a dedicated blank target disk, at least 6,000,000,000 bytes. The builder needs root privileges, debootstrap, the GPT/filesystem tools listed in the script, mount and chroot support, the specified Ubuntu packages, and a compatible static `/bin/qemu-x86_64` emulator. Run `bash deploy/appliance/build-rigpilotos-target.sh --help` for the argument list. Supply the full Agent archive, its version, the NVIDIA installer, the `rootfs-overlay` directory and your build revision. **The target device passed to both `--target-device` and `--erase-confirm` will be erased.**

## Configure one device

Configure each private device copy before first boot. In its root filesystem, create `/etc/rigpilotos-controller.env` as root with mode `0600`. Set `RIGPILOT_CONTROLLER_URL` to the HTTPS base URL of your own controller. This file contains a single line such as `RIGPILOT_CONTROLLER_URL=https://controller.example.invalid`. Replace the example hostname. The factory `user` and `root` accounts are password-locked. For local console access, set a unique password for `user` on the private copy with `passwd user` inside the mounted target chroot.

Generate a one-time enrollment token on your controller. On the separate provisioning partition, copy `rigpilot-enroll.example.json` to `rigpilot-enroll.json` and replace its placeholder. Boot with wired networking. The first-boot service waits for the controller, enrolls the Agent, and consumes the provisioning file after the installation completes. Keep any files containing real tokens or device credentials on that private device.

## Verify

`python -m pytest -q deploy/appliance/tests` exercises mock disks, package preflights and runtime policies on Linux. The `verify-unregistered-console.py` and `verify-enrolled-console.py` programs check a disposable test image through its serial console. Each accepts `--help` and uses the test machine’s explicitly configured local password from `RIGPILOTOS_TEST_PASSWORD`. The `memory/` tools record Linux memory and service measurements. These programs produce local reports that can contain system information.

## 中文说明

这里提供 Ubuntu 24.04 amd64 镜像的 Linux 构建配方、首次接入服务、NVIDIA 守护配置和验收工具。镜像、NVIDIA 安装程序、Ubuntu 安装包和部署身份需由构建者自行准备。

构建前在仓库根目录运行 `sh scripts/prepare-unix-modes.sh`，恢复构建器和镜像启动脚本的执行权限。

1. 先生成 Linux Agent 全量包，再准备以上三份 Ubuntu 软件包和对应版本的 NVIDIA 安装程序。构建器逐项校验哈希。使用专用空白目标磁盘，通过 `--help` 核对参数；构建器会擦除已确认的目标盘。
2. 在每台设备的独立磁盘副本中设置 `/etc/rigpilotos-controller.env`，文件归 root 所有，权限 `0600`，变量 `RIGPILOT_CONTROLLER_URL` 指向自己的 HTTPS 控制端。需要本地登录时，在该副本的 chroot 内用 `passwd user` 设置独立密码。
3. 将控制端新生成的一次性接入码填入配置分区的 `rigpilot-enroll.json`，连接有线网络后启动。在测试环境中使用上文的 pytest、串口或内存工具验收。
