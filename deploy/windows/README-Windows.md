# RigPilot Agent Windows 便携版

当前正式 Windows x64 发布物只有
`RigPilotAgent-X.Y.Z-Windows-x64-Portable.zip`。旧 Inno Setup 图形安装器已经停用，
项目不再包含其 `.iss` 或构建脚本。

## 新设备接入

1. 在控制端准备接入码：新设备用一次性注册码；全新目标机器继承已有设备身份时，
   为原设备生成一次性设备继承码。
2. 解压 ZIP；不要只从压缩包预览窗口直接运行。
3. 右键 `Install-Autostart.bat`，选择“以管理员身份运行”。
4. 尚无配置时先输入 HTTPS 控制端地址和设备备注名，再明确输入 `1`（新设备注册）
   或 `2`（继承已有设备），最后在隐藏输入框中输入相应接入码。已有本地身份会直接复用。

`Start-RigPilotAgent.bat` 也提供同一套首次接入选择和失败恢复流程。Windows 的模式选择
必须输入 `1` 或 `2`，直接回车不会自动选择新设备。

继承只恢复控制端设备身份；新机器的飞行表、矿工包、超频设置与驱动仍需重新检查配置，
跨 Linux/Windows 继承尤其如此。若本机已有受保护安装，新解压包不会用接入码覆盖其数据。

无人值守接入须由已提权的 PowerShell 进程提供且只提供一个接入码环境变量
`RIGPILOT_ENROLLMENT_TOKEN` 或 `RIGPILOT_REPLACEMENT_TOKEN`。首次尚无 `config.json`
时还须提供 `RIGPILOT_CONTROLLER_URL` 和 `RIGPILOT_DEVICE_NAME`，否则会在接入请求前失败。
非管理员入口不会跨 UAC 传递接入码；该入口会直接拒绝，请勿把码写入命令行或历史。

首次接入须从私有本机 NTFS 目录运行。分账户提权时，须由实际执行安装的管理员
在其私有目录解压并启动；从普通用户 Downloads 解压后再用另一个管理员账户通过 UAC
会在接入前拒绝。启动器先检查源目录的父链是否允许其他用户改写或删除目录项，再将
源程序目录设为 Administrators/SYSTEM 可写、将 `data` 及已有子文件设为
Administrators/SYSTEM-only 并逐项复核。共享盘或不能证明私有的
目录会在接入请求前失败；迁移后保留的旧目录仍受这些 ACL 保护。Windows 系统盘根目录可能由
TrustedInstaller 拥有，这是正常系统保护；不要为安装 Agent 改动盘根 owner 或 ACL。若仍报父目录
不可信，应按报错中的 Parent 路径检查解压位置。

控制端需先支持当前的新版注册恢复协议；已经成功注册过的设备保留原身份，不需要重新注册。
注册中断或本地保存失败时，保留现有 `data` 目录重新运行注册入口即可，Agent 会恢复该次注册；
结果未知时不要清空身份文件或改用新目录。恢复时只输入原模式的原接入码；不要改换模式或删除待恢复文件。
只有控制端明确确认此前未创建身份后，才使用新的有效注册码。

安装入口先完成所选接入方式，校验包内 manifest 后把程序、
配置和设备身份迁移到 `C:\ProgramData\RigPilotAgentPortable`，再建立名为
`RigPilotAgentPortable-<目录哈希>` 的 SYSTEM 开机计划任务。重复运行会保留 `data`、版本槽和身份。
`Remove-Autostart.bat` 只删除自启任务；`Stop-RigPilotAgent.bat` 只停止 Agent。

## 在线更新与 0.5.6 例外

当前源码只能构建自身版本。唯一例外是明确标记为 `BOOTSTRAP-ONLY.txt` 的 0.5.6
更新过渡包，它只用于让真实旧设备获得 `agent_update.v1`，不能作为正式版本上传。
后续正式更新使用版本槽、两次授权心跳确认和失败回滚。

## 从源码构建

在 Windows PowerShell 中运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\deploy\windows\build-windows-portable.ps1 -PythonPath (Get-Command python.exe).Source
```

构建器在项目 `work` 下创建隔离 venv，安装精确版本依赖并执行 `pip check`，再用
同一环境跑测试和 PyInstaller。冻结 EXE 必须由 Windows 运行时生成；完成后会回读
ZIP 内全部文件、校验 manifest，并在项目 `outputs` 写出 ZIP 与 `.sha256`。
发布拒绝覆盖同名文件；若 ZIP 已公开而侧车发布失败，会保留已公开文件并报错，
清理只处理本次私有暂存文件。只有 ZIP 和 `.sha256` 均齐全才算交付完成。

也可使用 `deploy\build-all-releases.ps1` 一并生成 Windows Agent 和 Controller。
此入口只处理 Windows 本地产物，无需 WSL；Linux 全量/在线包在 Ubuntu 构建环境中执行
`bash deploy/build-all-releases.sh` 生成。两个入口默认只构建和校验，不部署到控制端或设备。

## 经典源码或独立 EXE 计划任务

`deploy\install-agent-task.ps1` 和 `deploy\uninstall-agent-task.ps1` 是独立的经典
`RigPilotAgent` 任务入口。便携 ZIP 使用上文的 `.bat` 入口和自己的任务归属检查。
经典入口核对明确的执行命令、配置参数、工作目录和描述；同路径正常升级继续可用。
源码安装会保存已解析的 Python 绝对路径，旧任务保存的原 Python 命令只有在明确
源码目录、配置和可信描述同时一致时才能正常升级。

安装或卸载独立 EXE 时，在管理员 PowerShell 中提供实际安装位置和配置：

```powershell
.\deploy\install-agent-task.ps1 -AgentExePath C:\RigPilot\RigPilotAgent.exe -ConfigPath C:\RigPilot\config.json
.\deploy\uninstall-agent-task.ps1 -ExpectedAgentExePath C:\RigPilot\RigPilotAgent.exe -ExpectedConfigPath C:\RigPilot\config.json
```

源码任务卸载须提供原源码目录、配置及任务中保存的确切 Python 命令：

```powershell
.\deploy\uninstall-agent-task.ps1 -AllowLegacySourceTask `
  -ExpectedProjectRoot C:\RigPilotSource -ExpectedPythonPath C:\Python312\python.exe `
  -ExpectedConfigPath C:\RigPilotSource\agent\config.json
```

若旧源码任务保存的是 `python.exe`，明确把它作为 `ExpectedPythonPath`；脚本不会
根据已有任务的文件名推导归属。历史空描述任务只有在完整上下文匹配并明确传入
`-AllowLegacyEmptyDescription` 时才能卸载。无参数卸载在任务不存在时保持幂等；
存在同名任务时会要求上述安装上下文。

跨目录或源码/EXE 迁移时，安装命令还须提供旧任务的
`-ExpectedExistingConfigPath`，并选一组完整旧执行上下文：
`-ExpectedExistingAgentExePath`，或
`-ExpectedExistingProjectRoot` 与 `-ExpectedExistingPythonPath`。
只有这组明确上下文匹配，才可替换历史任务，包括该旧任务的空描述。

## 发布测试矩阵与强制门禁

Windows Agent 全量包构建器在隔离 venv 中运行所有共享 `agent/tests/test_*.py`，
排除依赖 Linux/systemd 的 `test_linux_*`（历史 `test_hiveos_*` 已随 0.8.58 的
HiveOS 移除一并删除）；Linux 类由 Linux
原生包构建验收。它还固定运行五个身份及安装协议文件：
`tests/test_agent_enrollment_recovery.py`、`tests/test_device_replacement_and_settlement.py`、
`tests/test_windows_installer.py`、`tests/test_windows_portable.py` 和
`tests/test_windows_portable_enrollment.py`。`test_miner_packages.py` 与
`test_pending_replacement.py` 必须在共享测试清单中，缺失会阻断构建；任何选中
测试失败也会停止打包。

测试通过后还必须校验冻结 EXE 的新设备注册与设备继承、本地再次心跳、AMD64
PE 类型及版本，再回读 ZIP 清单与逐文件 SHA-256 并写出外层 `.sha256`。
`-SkipTests`、`-SkipSmokeTest` 仅供本地诊断；使用它们的结果不能作为发布验收。
本轮目标仅是 Windows Agent 便携 ZIP，不以 Windows 控制端或全仓测试结果替代上述门禁。

Windows ARM64 主机需使用 **x64 Python** 和 .NET 8 SDK；构建器检查 `win-amd64`
解释器目标及最终 EXE 的 AMD64 PE 机器类型。64 位 ARM Python 不满足 x64 打包要求。
使用 Windows PowerShell 5.1 时，含中文的 `.ps1` 源码副本应采用 UTF-8 BOM 编码；
深层测试/依赖目录需要启用 Windows 长路径支持。
详见 [`BUILD-AND-TEST-GATES.md`](../BUILD-AND-TEST-GATES.md)。
