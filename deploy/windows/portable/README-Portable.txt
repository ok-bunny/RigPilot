RigPilot Agent 0.8.62 Windows 便携版
==================================

特点
----
- 解压即用，不安装到 Program Files。
- 默认不创建 Windows 服务或计划任务；需要时可单独安装/移除开机自启计划任务。
- 用户双击管理 BAT 时会先弹出一次 Windows UAC；不安装服务，但受保护数据、GPU OC 和 RandomX MSR 需要该权限。
- 配置、设备身份、日志、矿工包和托管运行文件都保存在当前目录的 data 文件夹。

首次运行（推荐安装自启）
----------------------
1. 在控制端准备接入码：全新设备生成一次性注册码；要在这台新机器继承已有设备身份，
   为原设备生成一次性设备继承码。继承码不能用于覆盖本机已有的受保护安装。
2. 双击 Install-Autostart.bat，并在 Windows UAC 中选择“是”。
3. 尚无配置时先输入 HTTPS 控制端地址和设备名称，随后选择 [1] 注册新设备或
   [2] 继承已有设备身份，再输入所选模式的接入码；模式必须明确输入 1 或 2。
   接入码输入时不显示，也不会写入配置、命令行或日志。
   data 中已有有效 state.json 时复用原身份，不再询问模式或接入码。
4. 输入自己控制端的 HTTPS 地址，例如 https://controller.example.invalid。示例地址只演示格式。
5. 接入成功后脚本新建 C:\ProgramData\RigPilotAgentPortable、迁移程序与 data、启用
   SYSTEM 开机自启，同时开启控制端下发重启和关机的本地权限。
6. 后台 Agent 会从 ProgramData 目录立即启动。
7. 如果注册已成功但迁移或自启安装失败，安装入口不会让设备保持“已注册却离线”：
   它会在本窗口以前台方式启动 Agent（提示 KEEP THIS WINDOW OPEN），设备保持在线、
   控制端可观测可下发；修复报错原因后再次运行 Install-Autostart.bat 会迁移同一身份。
   前台运行只是临时保持在线，不代表自启已安装；结束前台运行后安装入口仍返回失败。
   原始错误会先显示并写入 portable-launcher.log。迁移目标内部失败会先返回并回滚，
   再由原目录的交互入口提供前台运行，不会卡住迁移事务。

手动运行 Start-RigPilotAgent.bat 也使用同一套首次接入选择和失败恢复流程。
已经保存完整身份时，两种入口都直接复用该身份；不会用新接入码覆盖它。

继承只恢复控制端的设备身份，不复制旧机器上的本地飞行表、矿工包、超频设置或系统驱动；
特别是跨 Linux/Windows 继承后，要在新机器重新检查并配置这些本地项目。

受控自动化可在已提权的 PowerShell 进程中提供一个接入码环境变量：
RIGPILOT_ENROLLMENT_TOKEN（新设备）或 RIGPILOT_REPLACEMENT_TOKEN（继承）。
若尚无 config.json，还必须同时提供 RIGPILOT_CONTROLLER_URL 与 RIGPILOT_DEVICE_NAME；
缺少任一字段会在提交接入请求前失败，不会自动使用默认控制端。接入码不会作为命令行参数传递。
带接入码环境变量的非管理员入口会在 UAC 重启前拒绝，避免环境变量在提权过程中丢失；
请由已提权会话启动，不要将接入码写入命令历史。

首次注册或继承必须从支持 Windows ACL 的本机 NTFS 私有目录启动。发出接入请求前，
启动器会检查源目录全部上级路径是否允许其他用户改写或删除目录项；不能证明私有时会
拒绝，请重新解压到由执行安装的管理员拥有的私有本机目录。普通用户从自己的 Downloads
解压，再在 UAC 输入另一个管理员账户时，源目录仍属于原用户，将被安全检查拒绝；
应由该管理员先在其私有目录解压并启动。随后源程序目录只允许 Administrators/SYSTEM 写入，
data（包括已有配置、待恢复文件及子文件）只允许 Administrators/SYSTEM 读写，逐项复核
后才向控制端发请求。共享盘、网络盘或不支持 ACL 的文件系统会提前失败。迁移后旧目录
仍保留这些 ACL，传输及迁移核验完成后由管理员按当前资产规则处理旧副本。系统盘根目录由 TrustedInstaller 拥有时属于
正常 Windows 保护，不要改变盘根 owner/ACL；如果仍提示父目录不可信，按报错中的 Parent
路径检查具体解压目录，不要放宽共享目录权限。


如果画面显示 ConnectTimeout/连接超时，说明 Agent 尚未连通控制端，不是注册码格式错误。
先确认本机可打开控制端 HTTPS 地址，并检查 DNS、代理、防火墙和 443 端口；然后保留现有
data 目录，再运行原注册入口；若已发出请求但未收到结果，程序会恢复原注册。注册失败时
没有运行中的 Agent，Stop-RigPilotAgent.bat 会提示无需停止并正常结束。
普通注册待恢复时只输入原注册码；继承待恢复时只输入原设备继承码。不要切换模式、
改用新码或删除 data 中隐藏的 pending 文件。控制端明确确认此前未创建身份后，
才能重新开始全新注册。

安装或迁移失败时，除窗口中显示的错误外，故障原因也会追加写入两个纯文本日志：
data\logs\portable-migration.log（迁移协调器的每次成功/失败结果）和
data\logs\portable-launcher.log（启动器自身的失败记录）。迁移失败时窗口会直接
引用 migration 日志的最后几行；把这两个文件发给支持人员即可定位具体环节。
日志不含接入码或设备密钥，可安全提供。

也可以双击 Check-Connection.bat。它会读取 data\config.json 中已有的控制端地址；配置
尚未生成时，请先设置 RIGPILOT_CONTROLLER_URL 为自己控制端的 HTTPS 地址。检查结果依次显示 DNS、目标 TCP 端口和 HTTPS
/api/health，使用短超时且不会读取或显示注册码。HTTPS 证书验证始终开启，不会退回
裸 IP，也不会忽略证书错误。从受保护的 ProgramData 目录读取配置时会正常请求一次 UAC。

安装、自启与重复运行
--------------------
1. 新机器直接双击 Install-Autostart.bat；在前台明确选择新设备注册或继承已有设备。
   两种方式均先生成 data\config.json 和 data\state.json，再进入迁移和自启安装。
   已有完整本地身份时继续复用，不重新注册。若本机 ProgramData 已有受保护安装，
   新解压包没有对应身份时会在读取任何接入码前拒绝，避免覆盖其他设备的数据。
2. 在 Windows UAC 中选择“是”。如果当前目录不是
   C:\ProgramData\RigPilotAgentPortable，脚本会自动迁移程序、设备身份、配置、日志、
   矿工包和飞行表；不需要手工进入 ProgramData，也不需要再复制第二次。
3. 如果该 Agent 正在托管挖矿，迁移器会先按 PID、可执行文件路径和启动时间验证后停止
   该矿工；迁移完成后使用新 ProgramData 路径和原活动飞行表自动重新启动。其他进程不动。
4. 安装成功后会立即从受保护目录启动 Agent；以后 Windows 启动时以 SYSTEM 和最高权限
   启动的也只会是 ProgramData 副本。普通重启只启动 Agent，不会自行启动挖矿任务。
5. 迁移器保留原目录供迁移核验，核验完成后应按资产规则退役，不作为回退入口；迁移成功后不要再从原目录运行 Start/Install；日常
   停止、重启挖矿和 Agent 更新均从控制端进行。
6. 在受保护目录重复运行 Install-Autostart.bat 只会更新同一个计划任务，不会重复创建。
   不再需要时应运行受保护目录中的 Remove-Autostart.bat；它只移除自启，不删除数据。
7. 计划任务和启动器都设置了防重复机制。Agent 已运行时再启动会提示已运行。

所有用户入口都应通过 BAT 运行，不要直接双击或手工调用
portable-agent.ps1。portable-entry.ps1 会在旧版受信启动器读取受保护 data 前
先完成 UAC，并且只传递固定白名单动作。

注意：SYSTEM 开机任务本身没有交互窗口。首次接入交互由 Install-Autostart.bat
启动的前台流程完成；接入成功后才会创建 SYSTEM 任务。

启用远程重启/关机
-----------------
- Install-Autostart.bat 现在是唯一需要运行的安装入口：它会在注册自启前，以原子替换方式
  把现有 data\config.json 的 allow_reboot 和 allow_shutdown 设为 true，同时保留设备身份、
  控制端地址、矿工包、飞行表及其他本地安全限制。全新便携配置也默认启用这两个开关。
- Enable-Power-Actions.bat 的功能与安装入口重复，0.6.0 已删除该脚本。人工覆盖迁移或已确认的
  远程更新只会在受保护程序根内移除这个退休文件，不会触碰 data、versions 或未知文件。
- 配置在 Agent 下次启动时生效。首次迁移和重新安装会启动受保护副本；若 Agent 当时已在
  运行且计划任务忽略了重复启动，则最迟在下次 Agent 重启或 Windows 重启时生效。
- 便携启动器兼容 Windows 10 自带的 Windows PowerShell 5.1；无需安装 PowerShell 7。
- 启用后，拥有控制端相应权限的用户可以让该设备重启或关机，请只在受信控制端使用。

矿工包与飞行表
--------------
- 全新 0.6.16 便携配置默认启用托管挖矿、当前控制端下载和本地矿工运行目录。
- Web 飞行表可选择 XMRig、SRBMiner-MULTI、Pearl Miner、Seine，不需要填写 profile 或程序路径。
- 控制端会显示官方 GitHub 目录中的可用版本及 Windows/Linux 可用性；应用飞行表时，
  控制端先缓存所选 Windows 包，再下发给 Agent。未被飞行表或活动任务引用的第 4 个
  及更旧版本会在后续清理中删除。
- XMRig 可填写 CPU 挖矿核心数（线程数），留空由 XMRig 自动选择。
- Seine 支持 BNT（blocknet-argon2id）CPU 挖矿，线程留空自动选择；可选择 balanced、throughput 或 efficiency。
  该矿池协议不接受密码；API 未报告的 Share 保持未知，不显示为零。
- SRBMiner 的受支持双挖由同一个 SRBMiner 进程完成，主副项必须使用同一版本。
- SRBMiner 按型号 OC（例如 5080、5070、5060 Ti）会自动匹配同型号卡并直接生成官方 --gpu-coffset0/1、--gpu-cclock0/1、
  --gpu-moffset0/1、--gpu-mclock0/1、--gpu-plimit0/1 参数；Offset 与对应 Lock
  可以同时填写，双挖按 GPU 算法所在 A0/A1 槽位生成；没有匹配规则的卡保持默认 OC 并继续挖矿。
- 矿工包保存到 data\packages，运行配置、日志和进程记录保存到 data\miner-runtime。
- XMRig / SRBMiner 的本地 API 只监听 127.0.0.1；Agent 会向控制台上报实时运行状态、
  算力和 Share。手工矿工软件库仍是 legacy profile/监管服务兼容入口。
- 设备详情可单次返回最近 60 秒矿工日志，也可在实时日志窗口打开期间连续请求有界、脱敏快照；关闭窗口即停止。
- 设备列表可停止当前托管矿工，或按最后一次活动飞行表重启。停止会恢复 RigPilot 管理的
  SRBMiner 与 NVML OC；只有身份核验、停止和恢复完成后才报告成功。
- Agent 0.6.1 起可从设备详情执行“去除飞行表”：先验证并停止 RigPilot 自己管理的
  矿工，再删除固定的活动飞行表和状态文件；完成后设备显示未配置，也不会自动重启挖矿。

GPU OC
------
- 全新 0.6.16 便携配置默认把 allow_gpu_oc 设为 true，厂商仅允许 NVIDIA。

CPU 温度
--------
- 0.6.15 Windows x64 便携包内置 tools\cpu-temperature 下的只读辅助组件，固定使用
  LibreHardwareMonitorLib 0.9.6，只启用 CPU 传感器；它使用受支持 Windows 10/11
  已包含的 .NET Framework 4.7.2，不需要另外安装或打开硬件监控软件。
- Agent 以固定参数、短超时调用辅助组件，并缓存结果。只接受明确的 CPU
  Package/Tctl/Tdie（或受支持的回退 CPU 核心）温度；无传感器、权限不足或读数异常时
  控制台显示“温度未上报”，不会阻止心跳、飞行表或挖矿。
- 辅助组件及其锁定依赖的许可与来源说明随包保存在
  tools\cpu-temperature\THIRD-PARTY-NOTICES.txt 和 packages.lock.json。

网络地址
--------
- 0.6.7 同时上报局域网地址和设备网卡直接分配的公网地址，覆盖 Windows 与 Linux 受控端。
- 公网 IP 优先由控制端根据已认证心跳的连接来源记录；Agent 不请求第三方“查 IP”服务。
- 局域网列包含 RFC1918 IPv4、CGNAT 与 IPv6 ULA，不展示回环、链路本地或组播地址。

远程更新（agent_update.v1）
---------------------------
- 仅支持安装到固定 ProgramData 目录的 Windows x64 便携版；源码前台运行不会声明此能力。
- SYSTEM 自启和远程更新最终目录必须是 C:\ProgramData\RigPilotAgentPortable。
  0.5.6 过渡包允许直接覆盖到已注册的桌面/旧便携目录，再运行一次
  Install-Autostart.bat 自动迁移；不会把普通用户可写目录登记为 SYSTEM 任务。
- 安装自启时程序文件 ACL 只允许 Administrators/SYSTEM 写入；data（含设备密钥）
  仅 Administrators/SYSTEM 可读写。
- 更新包由控制端 TLS 与设备认证保护，校验整个 ZIP 的 SHA-256、精确文件清单和
  每个文件摘要；当前尚无商业 Authenticode/非对称发布签名。
- 新版写入独立 versions 槽，保留 data；连续两次成功连接控制端后才确认，启动或
  回连失败会自动恢复旧版本并上报失败。
- 确认成功后，Agent 会把非固定程序文件同步到受保护根，并按旧/新 manifest 差异删除退休文件。
  删除目标必须同时满足“旧 manifest 声明、而新 manifest 不再声明”，或属于明确退休白名单；
  data、versions 和未知文件永远不在删除集合中。
- agent_update.v1 的 portable-agent.ps1 与 RigPilotAgentUpdater.ps1 是 ACL 锁定的固定受信对，
  远程更新不会替换它们；实际根清单会记录它们当前的真实哈希，后续更新仍可连续执行。
- 0.8.62 完整包改进失败回滚：交接前确认旧程序与原设备身份，失败后仅对该次更新做
  有限恢复；原 Agent 仍在运行时不另开实例。正常停止不会触发恢复，显式执行
  Stop-RigPilotAgent.bat 或 Remove-Autostart.bat 会取消该安装仍在途的更新启动意图。
- 此改进属于完整包中的受信脚本。已安装旧受信脚本的设备仅在线升级 EXE，不能获得
  新脚本的恢复保障；不能把本次新包发布说成已修复旧设备上此前中断的更新。
- 已有设备应在确认没有在途更新后，由管理员从原受保护目录停止 Agent，并将校验通过
  的完整包程序文件覆盖到同一 C:\ProgramData\RigPilotAgentPortable。保留原 data、versions
  及原身份，不复制其他设备的数据，也不删除有效的 active-agent.json。随后从这个目录
  运行 Install-Autostart.bat，重建程序权限和自启/更新恢复任务。
- 若保留的有效指针仍选择旧版本槽，本次原位完整包维护更新了受信脚本，实际运行的
  Agent 仍可能是该旧槽；确认原身份在线后，再由用户通过正常升级入口选择新版本。
  原本没有版本指针的安装会运行完整包根目录的新 EXE。验收以实际心跳版本为准。
- 不要在已有受保护安装时从一个没有对应身份的新解压目录运行首次接入，也不要为通过
  预检删除 config.json/state.json。发现未完成更新、旧程序缺失或身份不一致，应先调查
  原事务，不能靠反复覆盖、重注册或删除版本槽恢复。
- 真正的 0.5.6 不支持该命令，需要先人工保留 data 引导一次。构建脚本的
  -ReleaseVersion 0.5.6 只生成带 BOOTSTRAP-ONLY.txt 的内部一次性引导包，不得上传
  到版本库或作为正式 0.5.6 发布。
- SRBMiner 的 GPU 飞行表可按型号填写 Core Clock Offset、Lock Core Clock、
  Memory Clock Offset、Lock Memory Clock 和 Power Limit，并通过上述 SRBMiner
  A0/A1 参数应用。
- 设备详情中每张 NVIDIA 卡的“OC 设置”只提供 NVML 的 Power Limit、Core Clock Offset、
  Lock Core Clock、Memory Clock Offset、Lock Memory Clock 和 Fan Speed 六项；
  不支持 AMD 调节；控制台不再保留独立 GPU OC 侧栏。
- Start-RigPilotAgent.bat 会自动请求管理员权限；是否能设置仍取决于 NVIDIA 型号、
  驱动和 NVML 支持，以及 data\config.json 中显式设置的本地上下限约束。
  默认不设额外的软件功耗上下限，实际硬件范围仍须通过核验。
- 错误功耗、频率或固定风扇可能造成崩溃、过热或硬件损坏。先用单卡和保守参数测试，
  保持独立温度监控；不需要 OC 时应把 allow_gpu_oc 改回 false。

从 0.4.x 便携版原地升级
------------------------
- 替换程序文件但保留 data 时，启动器只自动补充 managed_mining_enabled=true 和
  managed_miner_runtime_dir="miner-runtime"；不会覆盖其他已有安全设置。
- 旧配置中的 allow_gpu_oc 通常仍为 false。需要飞行表 OC 时请先停止 Agent，审核
  本地范围后手工改为 true；保留现有 data 和设备身份。
- 如果旧配置关闭了 allow_miner_downloads 或未配置 miner_packages_dir，也需要管理员
  自行审核并开启/补充；升级逻辑不会悄悄放宽这些设置。

更换控制端或重新注册
--------------------
当前便携入口不支持在既有受保护安装内直接更换控制端或重新注册。
不要删除 ProgramData 副本的 config.json/state.json 后重跑首次接入；启动器会拒绝
覆盖仍存在的受保护安装。需要先停止并退役既有受保护安装，再从新的私有解压目录
使用完整包进行首次接入。若上一次注册或继承结果尚未确认，先保留现有 data 目录，
在原控制端恢复原尝试，不得删除 pending 文件或开始新的身份请求。

安全提示
--------
- data\state.json 包含当前设备的独立凭据。注册后不要把整个便携目录发给别人。
- data 目录内隐藏的 pending 注册恢复文件同样包含本次注册的私有凭据，不能分享或外发。
- 首次接入必须从支持 Windows ACL 的本机 NTFS 目录启动，不能从共享目录、网络盘或
  不支持文件权限的公共 U 盘执行注册或继承。
- 一次性注册码只通过当前 PowerShell 进程环境传给 Agent。

额外参数（Agent 0.8.31）
-----------------------
- XMRig、SRBMiner、Pearl Miner 和 Seine 支持每项额外参数文本，最多 4096 字符。
- 引号可包裹含空格的值，未知的新软件参数按原参数列表传入，不经过 shell。
- 钱包、矿池、worker、设备、API、配置/日志路径及受管 OC 参数仍使用飞行表字段。
- 清空文本可移除额外参数；独立双挖各自使用对应文本，SRB 集成双挖按主、副顺序追加。
- lolMiner 和 ForgeMiner 已退役；有完整归属证明的旧进程仍可安全停止。
