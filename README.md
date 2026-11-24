# RigPilot

[简体中文](README.zh-CN.md) · [Getting started](docs/getting-started.md)

RigPilot is a self-hosted dashboard for Windows and Ubuntu mining rigs. It brings device health, hashrate and flight sheets into one place. Each rig runs an Agent that connects to your controller. You can enroll one machine, confirm its heartbeat and check its hardware before configuring any mining software.

| In the dashboard | What you can do |
| --- | --- |
| Devices | Check when a rig last reported, inspect its CPU and memory, and see GPU readings where the hardware and driver expose them. |
| Mining | Track reported hashrate and work. A flight sheet brings together a miner, its version, a pool and a public payout address for selected rigs. |
| Access | Register a rig with a short-lived, single-use code. Assign viewer, operator or administrator roles and review the activity log. |

## Try it with one device

Start the controller with Docker Compose on a machine you manage. Run the Agent from source on the same machine for the first heartbeat, or connect a second device to your HTTPS endpoint. The guide includes Bash and PowerShell commands, the Chinese labels used in the dashboard, and a check for each step.

[Follow the getting-started guide](docs/getting-started.md)

## Source layout

| Directory | Contents |
| --- | --- |
| [`server/`](server/) | FastAPI controller, SQLite storage, browser interface and shared miner definitions. |
| [`agent/`](agent/) | Python Agent for Windows and Ubuntu, hardware inventory and heartbeat client. |
| [`docs/`](docs/) | [English guide](docs/getting-started.md) and [中文教程](docs/getting-started.zh-CN.md). |

Source versions are recorded in the [controller](server/__init__.py) and the [Agent](agent/version.py).
