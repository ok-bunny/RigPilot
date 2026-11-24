# RigPilot

[English](README.md) · [入门教程](docs/getting-started.zh-CN.md)

RigPilot 是面向 Windows 和 Ubuntu 矿机的自托管管理平台。每台设备上的 Agent 主动连接你自己的控制端，将设备状态和算力汇总到同一个看板。第一次使用时，可以只接入一台机器，确认心跳和硬件信息，再决定是否配置挖矿软件。

| 在控制台中 | 可以做什么 |
| --- | --- |
| 设备 | 看设备最后何时在线，检查 CPU 和内存，并查看硬件和驱动能够上报的 GPU 指标。 |
| 挖矿配置 | 查看上报的算力和工作量。把矿工软件、版本、矿池和公开收款地址组合成一份飞行表，应用到选定设备。 |
| 接入与权限 | 使用短时有效的一次性注册码接入设备，按查看者、操作员或管理员分配权限，并查看活动记录。 |

## 从一台设备开始

在自己管理的主机上用 Docker Compose 启动控制端，再在同一台主机运行源码版 Agent。也可以让另一台设备通过 HTTPS 接入。教程提供 Ubuntu Bash 和 Windows PowerShell 命令，以及每一步应当看到的结果。

[阅读入门教程](docs/getting-started.zh-CN.md)

## 源码目录

| 目录 | 内容 |
| --- | --- |
| [`server/`](server/) | FastAPI 控制端、SQLite 存储、浏览器界面和共用的矿工软件定义。 |
| [`agent/`](agent/) | 适用于 Windows 和 Ubuntu 的 Python Agent、硬件采集与心跳客户端。 |
| [`docs/`](docs/) | [中文教程](docs/getting-started.zh-CN.md)和[英文教程](docs/getting-started.md)。 |

当前源码版本见[控制端](server/__init__.py)和 [Agent](agent/version.py) 的版本文件。
