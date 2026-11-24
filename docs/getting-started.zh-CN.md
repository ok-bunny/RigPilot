# 接入第一台 RigPilot 设备

[English](getting-started.md) · [项目简介](../README.zh-CN.md)

本教程会让一台名为 `demo-rig` 的设备出现在你的控制台里，并持续上报心跳。初次体验时，可以把控制端和 Agent 都放在同一台 Ubuntu 或 Windows 电脑上。准备 Git、带 Compose v2 的 Docker，以及带 `venv` 和 `pip` 的 Python 3.12。Windows 上的 Docker 选择 Linux 容器模式。Agent 用普通用户运行即可，这次体验也用不到独立显卡、矿工软件、钱包或矿池。

## 1. 启动控制端

克隆项目并进入目录。后续命令均从仓库根目录执行。

```text
git clone https://github.com/ok-bunny/RigPilot.git
cd RigPilot
```

在新克隆的目录里创建自己的配置文件。Ubuntu 使用以下命令。

```bash
umask 077
cp .env.example .env
nano .env
```

Windows 使用 PowerShell。

```powershell
Copy-Item .env.example .env
notepad .env
```

用密码管理器生成新的管理员密码，填入 `RIGPILOT_ADMIN_PASSWORD`。例如，32 位随机字母数字串可直接用于 Compose，无须转义。将密码保存在密码管理器中，稍后登录时会用到。本教程保留默认用户名 `RIGPILOT_ADMIN_USERNAME=admin`。保存并关闭编辑器，然后启动控制端。

```text
docker compose up -d --build
docker compose ps
```

打开[本机健康检查页](http://127.0.0.1:8080/api/health)，确认返回的 JSON 含有 `"status":"ok"` 和 `"enrollment_protocol":"rigpilot.enrollment.v2"`。再用浏览器打开[本机控制台](http://127.0.0.1:8080/)，以 `admin` 登录。`.env` 中的密码用于首次建库。如果在首次设置向导中修改了密码，之后请用新密码登录。

## 2. 准备 Agent

同机体验可以直接使用当前仓库。若 Agent 在另一台设备运行，请在该设备上也克隆项目，并按本节后面的说明配置 HTTPS。保留完整仓库目录，Agent 会从 `server/` 导入共用模块。

**Ubuntu（Bash）**

```bash
python3.12 -m venv .venv-agent
.venv-agent/bin/python -m pip install -r agent/requirements.txt
.venv-agent/bin/python -m agent --version
install -d -m 700 "$HOME/.local/share/rigpilot-demo"
cfg="$HOME/.local/share/rigpilot-demo/agent.json"
nano "$cfg"
```

**Windows（PowerShell）**

```powershell
py -3.12 -m venv .venv-agent
.\.venv-agent\Scripts\python.exe -m pip install -r agent\requirements.txt
.\.venv-agent\Scripts\python.exe -m agent --version
$agentDir = Join-Path $env:LOCALAPPDATA "RigPilotDemo"
New-Item -ItemType Directory -Force -Path $agentDir | Out-Null
$cfg = Join-Path $agentDir "agent.json"
notepad $cfg
```

版本命令应输出 `RigPilot Agent` 和版本号。将以下内容粘贴到刚打开的 `agent.json`，以 UTF-8 保存。接下来的注册命令在同一个终端里执行。

```json
{
  "server_url": "http://127.0.0.1:8080",
  "name": "demo-rig",
  "state_file": "identity.json",
  "dry_run": true
}
```

**如果 Agent 在另一台设备**，请把 `server_url` 改成自己的控制端 HTTPS 地址，例如 `https://controller.example.org`。上面的回环地址始终指向 Agent 所在的那台机器。在控制端主机上配置反向代理，让它使用 Agent 信任的证书，并将请求转发到 `127.0.0.1:8080`。在 `.env` 中把 `FORWARDED_ALLOW_IPS` 设置为 Uvicorn 实际看到的代理来源 IP，设置 `RIGPILOT_REQUIRE_HTTPS=true`，再运行 `docker compose up -d`。如使用私有证书机构，在 Agent 配置中把 `ca_bundle` 指向该机构的 PEM 证书文件。先在设备上确认能够打开这个 HTTPS 地址的 `/api/health`，再申请注册码。

## 3. 注册并发送第一次心跳

1. 在控制台点 **接入设备**。也可以从首次设置向导进入 **设备接入 → 打开设备接入**。
2. 选择 **接入新设备**，将设备显示名称填为 `demo-rig`，选择有效时长，点击 **生成注册码**。请在窗口关闭前复制，注册码只显示一次。
3. 在 Agent 终端运行对应平台的命令。在隐藏输入提示处粘贴注册码并回车。这一步会实际注册设备、生成设备身份并发送心跳。试运行模式也会完成注册。

**Ubuntu**

```bash
read -r -s -p '设备注册码: ' RIGPILOT_ENROLLMENT_TOKEN
echo
export RIGPILOT_ENROLLMENT_TOKEN
.venv-agent/bin/python -m agent --config "$cfg" --once --dry-run
unset RIGPILOT_ENROLLMENT_TOKEN
```

**Windows**

```powershell
$code = Read-Host "设备注册码" -AsSecureString
$env:RIGPILOT_ENROLLMENT_TOKEN = [System.Net.NetworkCredential]::new("", $code).Password
try {
    .\.venv-agent\Scripts\python.exe -m agent --config $cfg --once --dry-run
} finally {
    Remove-Item Env:RIGPILOT_ENROLLMENT_TOKEN
    $code.Dispose()
}
```

成功后，在控制台的 **设备** 页面应能看到 `demo-rig`。打开详情，核对操作系统和最后在线时间。Agent 会把 `identity.json` 保存到 `agent.json` 同一目录，之后用它发送心跳。请把这个私有目录留在原设备上，限制为运行用户和管理员可访问。

## 4. 保持在线并查看读数

在原终端再次启动 Agent。这次省略注册码和 `--once`，让它在前台持续运行。示例配置会继续启用试运行模式。

| 平台 | 命令 |
| --- | --- |
| Ubuntu | `.venv-agent/bin/python -m agent --config "$cfg"` |
| Windows | `.\.venv-agent\Scripts\python.exe -m agent --config $cfg` |

观察设备列表的 **状态/最后在线时间** 是否随多次心跳更新。Agent 采集到数据后，会显示 CPU 和内存读数。有矿工软件运行并上报统计时，才能看到算力。按 Ctrl+C 结束 Agent。需要停止控制端时，在控制端主机运行 `docker compose down`。数据库和控制端密钥保存在命名数据卷内，下次执行 `docker compose up -d` 时可继续使用。

## 准备第一份飞行表（可选）

心跳稳定后，可以在控制台配置挖矿参数。先**新建**一份飞行表，保存后查看配置，再决定是否分配设备。

1. 打开 **钱包 → 添加钱包**，选择币种，填入该币种的公开收款地址。签名私钥和助记词留在自己的钱包里。
2. 打开 **矿池 → 添加矿池**，填写该矿池为所选币种公布的接入地址、端口和必要的连接参数。
3. 打开 **飞行表 → 新建飞行表**，为飞行表命名。选择矿工软件及兼容版本、钱包和已保存的矿池，核对算法后点击 **保存飞行表**。回到列表，确认新表已出现。选择矿池时，其当时的连接参数会复制到表里。

到这里就完成了配置练习。将飞行表应用到设备是单独的操作，需要 Agent 事先启用相应能力。之后若修改一份已分配飞行表的运行参数，正在使用它的设备可能会同步这些改动。

## 常见问题

| 现象 | 检查方法 |
| --- | --- |
| 浏览器打不开控制台 | 在控制端主机运行 `docker compose ps` 和 `docker compose logs --tail 50 rigpilot`。确认首次启动所需的密码已填写，且 8080 端口可用。 |
| 改了 `.env` 还是登录失败 | 已有数据库使用已保存的账号。用上次在控制台设置的密码登录。 |
| Agent 报地址或证书错误 | 从设备本身检查控制端地址、端口和证书域名。如用私有 CA，也核对 `ca_bundle` 的 PEM 路径。 |
| 注册中途断线 | 保留原配置、身份目录和待完成注册文件，用同一配置重试。需要再次输入注册码时，使用原来的码。收到注册码无效或过期的明确拒绝后，再生成新码。 |
| 设备出现一次后又离线 | `--once` 只发一次心跳就退出。使用第 4 步的前台命令持续上报。 |
