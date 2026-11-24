# Controller 实际发布包 HTTP 验收

`smoke-controller-release.py` 是可复用组合入口，`smoke-controller-target.py`
保留已验收的账户、安装向导、旧数据库升级、未知矿工拒绝和静态资源 HTTP 场景。
当前修复场景另验证 workload snapshot 的完整 ACK、重复/冲突/退休 epoch、
以及 disable/enable、继承/激活和删除后的身份一致性（0.8.48 起远程会话场景移除，
身份守卫改由 agent_identity_lifecycle 场景承载）。并发 HTTP 检查不宣称复现了确定的线程交错；该部分由源码定向回归验证。
OC 场景用合成 Agent 回执验证 R2 NVML15/16 暂停自动重试、保存值保留、同值新意图与旧回执隔离、
047 精确文本兼容及控制端进程重启后的阻断持久化。回执中的 UUID 和指纹表示请求意图；
该 HTTP 检查不宣称验证了 Agent 生产者或真实 GPU 恢复。
它们是构建/验包工具，放在 `deploy` 根目录；不属于 Controller 生产 payload，
不向 `deploy/controller-runtime` 增加文件，也不修改 builder 的精确允许集合。

## 正式调用顺序

1. 由该批唯一构建负责人在获准的 Ubuntu 构建环境执行现有
   `build-controller-release.py`（或 `build-all-releases.sh --skip-linux`），
   再执行 `verify-release-artifacts.py --skip-windows --skip-linux`，传入实际
   `--output-dir` 和 `--controller-version`。聚合入口现有的静态验包不等同于 HTTP 验收。
2. 冻结本次归档、侧车、manifest、`server/main.py` 以及两个 smoke 脚本的 SHA-256。
   核对实际使用的 Ubuntu 22.04 amd64 容器及其 image ID。
   运行记录注明容器的目标用户空间和任何实际使用的翻译层。
3. 在该 Linux 环境调用下面的组合入口。每次使用新的 `smoke-output`，失败重跑也不能覆盖旧证据。
   `--work-root` 可以是本次 native build 的工作根，已有 `extracted` 会按归档逐文件复核。
   若尚无 `extracted`，入口安全写入已验证的普通文件，不调用 `tar.extract*`。

```bash
python3 deploy/smoke-controller-release.py \
  --archive "$CONTROLLER_ARCHIVE" \
  --archive-sha256 "$CONTROLLER_SHA256" \
  --manifest-sha256 "$CONTROLLER_MANIFEST_SHA256" \
  --main-sha256 "$CONTROLLER_MAIN_SHA256" \
  --work-root "$NATIVE_WORK_ROOT" \
  --image-id "$QUALIFIED_IMAGE_ID" \
  --smoke-script-sha256 "$CONTROLLER_SMOKE_SCRIPT_SHA256" \
  --environment-note "$ACTUAL_TEST_ENVIRONMENT"
```

这些变量来自本次已冻结输入及实际环境，不复制历史版本的值。
`--manifest-sha256`、`--main-sha256` 是可选附加冻结校验；实际版本、main 哈希、
cache revision、wheelhouse 和 lock 路径均从已校验归档派生。归档外层哈希和侧车、
manifest 精确成员与逐文件哈希、路径/链接/权限/资源边界、Python 3.10 语法、
runtime 身份及 lock/wheel 名称版本哈希闭包均先验证。依赖数量由当前闭包派生。

向构建机传输这两个脚本（或包含它们的已冻结源码）时，两个文件放在同一目录；
另放目录时用 `--scripts-dir` 指向该目录。先按该批材料清单确认脚本哈希，
新增容器镜像或构建环境须实际验证其资格。

## 运行边界与输出

入口使用 `docker run --pull never --network none`，不发布端口，不挂载生产数据。
只读挂载实际解包 payload 与 smoke 脚本，在私有 `smoke-output/venv` 中
用归档自带 wheelhouse 执行 `pip --no-index --only-binary=:all: --require-hashes`。
当前锁携带的依赖必须与实际 wheel 身份、版本和哈希完全一致。
容器中的实际 `server.main:app` 仅监听 `127.0.0.1`，脚本再次核对 runtime 平台、
Python/glibc 与 loopback 路由，并使用私有合成 SQLite 数据及合成账户/Agent 身份。
不会运行 Agent、矿工二进制或接触设备。

退出时停止本次创建的 Uvicorn 子进程。launcher 仅按随机独有 container 名称
检查其 owner label；必要时删除匹配的 container ID，并确认已停止。
保留该批工作目录中的归档、解包材料、venv 和工具链，供验收与归档。

输出位于 `$NATIVE_WORK_ROOT/smoke-output`：

| 文件 | 契约 |
| --- | --- |
| `report.json` | `rigpilot.controller.target-native-smoke.v1`，实际 HTTP 场景、静态字节和子进程停止证据 |
| `result.json` | `rigpilot.controller.target-native-smoke-launch.v1`，退出码、归档/manifest/main/cache/脚本哈希及容器清理证据 |
| `offline-pip.log` | 离线依赖安装结果 |
| `container.stdout.log`、`container.stderr.log` | launcher 只写本次容器输出 |
| `uvicorn.log` | 本次 fresh/reopened/legacy 实际进程日志的组合 |
| `uvicorn-fresh.log`、`uvicorn-reopened.log`、`uvicorn-legacy.log` | 对应实际启动的独立日志 |

既有 native exporter 使用的 `status`、`source_inputs_stable`、`uvicorn_stopped`、
`checks_passed`、`manifest_sha256` / `source_manifest_sha256`、`main_sha256`、
`cache_version`、`container_cleanup.confirmed_stopped` 等键及输出相对路径保留。
新 exporter 应匹配通用 schema，并接受 smoke container 名称
`rigpilot-controller-smoke-[0-9a-f]{32}`；构建 container 名称仍由本批 builder 定义。
不得按固定场景数量判成功。

负责人在导出前须再次检查 `report.status == result.status == passed`、输入哈希稳定、
全部 Uvicorn 与本次容器停止，然后只删除本次输出根下已验证为普通目录的
`data` 和 `data-legacy`，记录 synthetic DB 已移除。保留源码、构建资料、脚本、
日志和 venv；正式导回报告不包含这些合成数据库。失败时保留证据并先处理本次
进程清理结果，不删除或复用其他工作目录。

本地 `--help`、AST/compile 及安全解包 helper 检查只验证工具本身，
不代表构建环境、Docker 或实际包 HTTP 已通过。报告中的静态 HTML/JS 字节存在
也不构成新增浏览器布局或交互验收。
