#!/bin/sh

# Login summary for the appliance's local interactive user.  Keep this file
# silent for scp, remote commands and services: it is sourced by /etc/profile.
case "$-" in
    *i*) ;;
    *) return 0 ;;
esac
[ -t 1 ] || return 0
[ "$(id -un 2>/dev/null)" = "user" ] || return 0

# Keep login/PAM in charge of authentication. Only an authenticated local VT
# gets a font-rendering terminal; SSH, serial and nested shells stay unchanged.
if [ -z "${SSH_CONNECTION+x}${SSH_CLIENT+x}${SSH_TTY+x}" ] &&
   [ -z "${RIGPILOTOS_FBTERM_SESSION+x}" ] &&
   [ -c /dev/fb0 ] && [ -r /dev/fb0 ] && [ -w /dev/fb0 ] &&
   command -v fbterm >/dev/null 2>&1; then
    case "$(tty 2>/dev/null)" in
        /dev/tty[1-6])
            RIGPILOTOS_FBTERM_SESSION=1 LC_ALL= LC_CTYPE=C.UTF-8 \
                fbterm --font-names="WenQuanYi Micro Hei Mono" -- /bin/bash --login || :
            # FbTerm may return success without starting its child shell.
            # Always keep the authenticated parent shell available afterward.
            ;;
    esac
fi

rigpilotos_release_value() {
    awk -F= -v wanted="$1" '$1 == wanted { sub(/^[^=]*=/, ""); print; exit }' "$2" \
        2>/dev/null
}

rigpilotos_name="$(rigpilotos_release_value NAME /etc/rigpilotos-release)"
rigpilotos_version="$(rigpilotos_release_value VERSION /etc/rigpilotos-release)"
rigpilotos_revision="$(rigpilotos_release_value BUILD_REVISION /etc/rigpilotos-release)"
ubuntu_version="$(rigpilotos_release_value VERSION_ID /etc/os-release)"
nvidia_version="$(rigpilotos_release_value NVIDIA_DRIVER_VERSION /etc/rigpilotos-release)"

printf '\n%s %s (%s)\n' \
    "${rigpilotos_name:-RigPilotOS}" \
    "${rigpilotos_version:-unknown}" \
    "${rigpilotos_revision:-unknown}"
printf 'Ubuntu %s | Kernel %s | NVIDIA %s\n' \
    "${ubuntu_version:-unknown}" "$(uname -r)" "${nvidia_version:-unknown}"

if systemctl is-enabled --quiet rigpilot-agent.service 2>/dev/null; then
    if systemctl is-active --quiet rigpilot-agent.service 2>/dev/null; then
        printf '设备注册：已注册，Agent 正在运行\n'
    else
        printf '设备注册：已注册，Agent 当前未运行\n'
        printf '  启动 Agent：sudo rigpilot-control start\n'
    fi
else
    printf '设备注册：未完成\n'
    printf '  输入新注册码：sudo rigpilot-control enroll\n'
    printf '  重试 U 盘文件：sudo rigpilot-control enroll-media\n'
fi

# Do not invoke nvidia-smi at all on hosts without an NVIDIA display device;
# this keeps CPU-only appliances free of NVRM/error noise.
rigpilotos_has_nvidia_gpu=false
for rigpilotos_vendor_file in /sys/bus/pci/devices/*/vendor; do
    [ -r "$rigpilotos_vendor_file" ] || continue
    [ "$(cat "$rigpilotos_vendor_file" 2>/dev/null)" = "0x10de" ] || continue
    rigpilotos_class_file="${rigpilotos_vendor_file%/vendor}/class"
    [ -r "$rigpilotos_class_file" ] || continue
    case "$(cat "$rigpilotos_class_file" 2>/dev/null)" in
        0x03*) rigpilotos_has_nvidia_gpu=true; break ;;
    esac
done

if [ "$rigpilotos_has_nvidia_gpu" = true ] && command -v nvidia-smi >/dev/null 2>&1; then
    rigpilotos_gpu_rows="$(timeout 2s nvidia-smi \
        --query-gpu=index,name,temperature.gpu,utilization.gpu,power.draw,memory.used,memory.total \
        --format=csv,noheader,nounits 2>/dev/null)" || rigpilotos_gpu_rows=""
    if [ -n "$rigpilotos_gpu_rows" ]; then
        printf 'GPU 状态：\n'
        printf '%s\n' "$rigpilotos_gpu_rows" | while IFS=, read -r \
            rigpilotos_index rigpilotos_gpu_name rigpilotos_temp rigpilotos_util \
            rigpilotos_power rigpilotos_mem_used rigpilotos_mem_total; do
            printf '  GPU%s %s |%s°C | 利用率%s%% | 功耗%s W | 显存%s/%s MiB\n' \
                "$rigpilotos_index" "$rigpilotos_gpu_name" "$rigpilotos_temp" \
                "$rigpilotos_util" "$rigpilotos_power" \
                "$rigpilotos_mem_used" "$rigpilotos_mem_total"
        done
    else
        printf 'GPU 状态：检测到 NVIDIA 显卡，但驱动暂未返回状态\n'
    fi
elif [ "$rigpilotos_has_nvidia_gpu" = true ]; then
    printf 'GPU 状态：检测到 NVIDIA 显卡，但 nvidia-smi 不可用\n'
else
    printf 'GPU 状态：未检测到 NVIDIA 显卡\n'
fi

printf '常用命令：rigpilot-control status | enroll | enroll-media | logs | network | driver\n\n'

unset rigpilotos_name rigpilotos_version rigpilotos_revision ubuntu_version
unset nvidia_version rigpilotos_has_nvidia_gpu rigpilotos_vendor_file
unset rigpilotos_class_file rigpilotos_gpu_rows
unset -f rigpilotos_release_value 2>/dev/null || true
