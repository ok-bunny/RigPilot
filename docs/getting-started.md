# Your first RigPilot device

[简体中文](getting-started.zh-CN.md) · [Project overview](../README.md)

This walkthrough ends with a device named `demo-rig` reporting to your dashboard. For the shortest setup, run the controller and Agent on the same Ubuntu or Windows machine. You need Git, Docker with the Compose v2 plugin, and Python 3.12 with `venv` and `pip`. On Windows, use Docker in Linux container mode. Use a regular user account for the Agent. A GPU, miner, wallet and pool are all optional for this exercise.

## 1. Start the controller

Clone the project and open its directory. Run the remaining commands from this directory.

```text
git clone https://github.com/ok-bunny/RigPilot.git
cd RigPilot
```

On a fresh checkout, create your private settings file. On Ubuntu, use the following commands.

```bash
umask 077
cp .env.example .env
nano .env
```

On Windows, use PowerShell.

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `RIGPILOT_ADMIN_PASSWORD` to a new, password-manager-generated value. A random alphanumeric string of 32 characters works with Compose without escaping. Save the password in your password manager for the next step. Leave `RIGPILOT_ADMIN_USERNAME` as `admin` for this tutorial. Save and close the editor, then start the controller.

```text
docker compose up -d --build
docker compose ps
```

Open the [local health endpoint](http://127.0.0.1:8080/api/health). The JSON response should contain `"status":"ok"` and `"enrollment_protocol":"rigpilot.enrollment.v2"`. Open the [local dashboard](http://127.0.0.1:8080/) and sign in as `admin`. The password in `.env` initializes a new database. If you change the password in the first-login wizard, use the new one on subsequent logins.

## 2. Prepare the Agent

Use the same checkout for a local Agent. For a separate device, clone the project on that device as well and configure HTTPS as described below. Keep the whole repository together because the Agent imports shared modules from `server/`.

**Ubuntu (Bash)**

```bash
python3.12 -m venv .venv-agent
.venv-agent/bin/python -m pip install -r agent/requirements.txt
.venv-agent/bin/python -m agent --version
install -d -m 700 "$HOME/.local/share/rigpilot-demo"
cfg="$HOME/.local/share/rigpilot-demo/agent.json"
nano "$cfg"
```

**Windows (PowerShell)**

```powershell
py -3.12 -m venv .venv-agent
.\.venv-agent\Scripts\python.exe -m pip install -r agent\requirements.txt
.\.venv-agent\Scripts\python.exe -m agent --version
$agentDir = Join-Path $env:LOCALAPPDATA "RigPilotDemo"
New-Item -ItemType Directory -Force -Path $agentDir | Out-Null
$cfg = Join-Path $agentDir "agent.json"
notepad $cfg
```

The version command should print `RigPilot Agent` and a version number. Paste the configuration below into the new `agent.json` and save it as UTF-8. Keep this terminal open for enrollment.

```json
{
  "server_url": "http://127.0.0.1:8080",
  "name": "demo-rig",
  "state_file": "identity.json",
  "dry_run": true
}
```

**For a separate device**, replace `server_url` with the HTTPS origin of your own controller, for example `https://controller.example.org`. The loopback address above always points to the machine running the Agent. On the controller host, have a reverse proxy with a certificate trusted by the Agent forward to `127.0.0.1:8080`. Set `FORWARDED_ALLOW_IPS` to the proxy's actual peer IP as seen by Uvicorn and `RIGPILOT_REQUIRE_HTTPS=true` in `.env`, then run `docker compose up -d`. For a private certificate authority, set `ca_bundle` in the Agent configuration to your PEM CA bundle. Verify that the device can open `/api/health` at the HTTPS origin before requesting a code.

## 3. Enroll and send one heartbeat

1. On the dashboard, select **接入设备** (Enroll device). In the first-login wizard you can also select **设备接入 → 打开设备接入**.
2. Select **接入新设备** (New device), enter `demo-rig` as the display name, select the validity period and click **生成注册码** (Generate code). Copy the code while the dialog is open. It is shown once.
3. In the Agent terminal, use the commands for your OS. Paste the code at the hidden prompt and press Enter. Registration creates a device identity even in dry-run mode.

**Ubuntu**

```bash
read -r -s -p 'Enrollment code: ' RIGPILOT_ENROLLMENT_TOKEN
echo
export RIGPILOT_ENROLLMENT_TOKEN
.venv-agent/bin/python -m agent --config "$cfg" --once --dry-run
unset RIGPILOT_ENROLLMENT_TOKEN
```

**Windows**

```powershell
$code = Read-Host "Enrollment code" -AsSecureString
$env:RIGPILOT_ENROLLMENT_TOKEN = [System.Net.NetworkCredential]::new("", $code).Password
try {
    .\.venv-agent\Scripts\python.exe -m agent --config $cfg --once --dry-run
} finally {
    Remove-Item Env:RIGPILOT_ENROLLMENT_TOKEN
    $code.Dispose()
}
```

After a successful run, look for `demo-rig` under **设备** (Devices). Open its details and check its OS and last-seen time. The Agent saves `identity.json` next to `agent.json` and uses it for subsequent heartbeats. Keep this private directory on its original device and accessible only to its user and administrators.

## 4. Watch the device stay online

Run the Agent again, without the code or the `--once` flag. It stays in the foreground. The example configuration keeps dry-run enabled.

| Platform | Command in the same terminal |
| --- | --- |
| Ubuntu | `.venv-agent/bin/python -m agent --config "$cfg"` |
| Windows | `.\.venv-agent\Scripts\python.exe -m agent --config $cfg` |

Watch **状态/最后在线时间** (Status / last seen) in the device list across successive heartbeats. CPU and memory readings appear as the Agent collects them. Hashrate requires a running miner that reports statistics. Press Ctrl+C to stop the Agent. To stop the controller, run `docker compose down` on its host. The named data volume keeps the database and controller secret for the next `docker compose up -d`.

## Optional first flight sheet

Once the heartbeat is stable, you can prepare mining settings in the dashboard. Creating a **new** flight sheet lets you review its choices before assigning any devices.

1. In **钱包 → 添加钱包** (Wallets → Add wallet), choose a coin and enter its public payout address. Keep signing keys and recovery phrases in your wallet.
2. In **矿池 → 添加矿池** (Pools → Add pool), enter your pool's published endpoint, port and any required connection parameters for that coin.
3. In **飞行表 → 新建飞行表** (Flight sheets → New), give the sheet a name. Pick a miner and compatible version, select the wallet and saved pool, review the algorithm, and click **保存飞行表** (Save). Verify the new entry in the list. The pool details are copied into the sheet at selection time.

This completes the configuration exercise. Applying the sheet is a separate device action that requires a suitably configured Agent. Later edits to an assigned sheet's runtime settings can synchronize to rigs already using it.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| The browser cannot open the dashboard | Run `docker compose ps` and `docker compose logs --tail 50 rigpilot` locally. Check that the initial password is set and that port 8080 is available. |
| Login fails after editing `.env` | An existing database uses its saved account. Sign in with the password last set in the dashboard. |
| The Agent rejects the address or certificate | Check the origin from the device itself, including the port and certificate hostname. For a private CA, check the PEM path in `ca_bundle`. |
| Registration was interrupted | Keep the same config, identity directory and pending enrollment journal. Retry using the same code if requested. After a definite rejection for an invalid or expired code, request a fresh one. |
| The device appears once, then goes offline | A run with `--once` exits after the heartbeat. Start the foreground command in step 4 to keep reporting. |
