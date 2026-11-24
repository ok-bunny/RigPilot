# RigPilot agent

This directory contains the cross-platform Python agent. It makes outbound-only
HTTPS requests, enrolls once, reports host health, and accepts only fixed command
types: diagnostics, on-demand miner logs, managed-miner stop/restart, allowlisted
service restart, reboot, shutdown, flight-sheet application, and GPU profile
application. It also contains fixed, typed managers for official XMRig,
SRBMiner-MULTI, and Pearl Miner packages. These miners are described by the
shared, immutable `miner-adapter.v1` registry, while execution and telemetry use
fixed handlers bundled with the Agent. The legacy flight-sheet wire format remains
accepted. An adapter cannot provide scripts, dynamic imports, executable paths,
or shell fragments.
The source configuration keeps host power, managed mining, downloads, and GPU
tuning disabled by default; the 0.5.8 and later Windows distributions opt in
to the managed-miner features described below. There is no general command
endpoint. Starting with 0.8.62 the Agent leaves miner restarts and GPU device selection
to the operator. It still reports miner faults and device inventory. Existing
miner self-healing settings are ignored when loading older configurations.

## Install and run

### Windows portable release

The supported Windows x64 distribution is the current
`RigPilotAgent-X.Y.Z-Windows-x64-Portable.zip`. Run `Install-Autostart.bat` as an
administrator: first enrollment still uses a controller-generated one-time
token, then the reviewed files, identity and configuration are migrated to
`C:\ProgramData\RigPilotAgentPortable` and a SYSTEM startup task is installed.
The obsolete Inno Setup application installer is no longer built or shipped.
See [`../deploy/windows/README-Windows.md`](../deploy/windows/README-Windows.md).

### Source installation

Use Python 3.10 or newer from the repository root. See the [getting started guide](../README.md) for first-time setup, TLS, and enrollment. After the first successful enrollment,
the token is no longer needed. The state file stores the agent ID, high-entropy
secret, and a bounded command-result cache. It is written atomically and set to
mode `0600` on POSIX. Protect the file with an appropriate Windows ACL when
running on Windows.

A new Agent first requires the controller to advertise
`enrollment_protocol=rigpilot.enrollment.v2` from `GET /api/health`. Upgrade the
controller first; there is no silent fallback to the legacy enrollment protocol.
An already-enrolled Agent keeps its identity and does not need to enroll again.
Before `POST /api/agent/enroll`, the Agent persists a private journal next to the
state file (`.<state filename>.enrollment-pending`) that records the original
attempt identity. If the reply is lost or the state cannot be saved, re-run with
the same configuration and data directory: the same pending proof resumes the
same attempt. If the first request never reached the controller, supply the
original token when prompted; a different token cannot replace a pending attempt.
Do not delete or recreate the journal on an unknown outcome. A
definite `enrollment_not_created` rejection that matches this attempt lets the
Agent clear the rejected pending attempt so you can retry with a valid token.
Protect the journal like the state file; raw enrollment tokens are not stored in
it.

Use `--once` for a single heartbeat and `--dry-run` to exercise enrollment,
telemetry, command validation, and result reporting without restarting a service,
writing a flight sheet, changing host power state, or touching a GPU. Diagnostics
remain active in dry-run mode.

Production URLs must use HTTPS. Certificate verification cannot be disabled;
`ca_bundle` may point to a private CA file. Plain HTTP is accepted only for a
loopback server, unless `allow_insecure_http` is explicitly enabled for a
development environment.

## Server protocol

- `POST /api/agent/enroll` with the one-time token and host identity, only after
  the `GET /api/health` enrollment-protocol check above; the pending journal
  makes an interrupted attempt resumable with the same identity.
- `POST /api/agent/heartbeat` with `X-Agent-ID` and `Authorization: Bearer ...`.
- `POST /api/agent/commands/{id}/result` with the same authentication headers.
- `GET /api/agent/miner-packages/{id}/download` with the same authentication
  headers for packages uploaded to this controller.
- `GET /api/agent/updates/{command-id}/download` for a single, explicitly
  targeted platform update archive. Redirects and cross-origin URLs are rejected.

The high-entropy bearer credential is protected by verified TLS. This design lets
the server store a one-way hash of the credential. Secrets and enrollment tokens
are never included in logs.

The agent accepts command objects shaped as `{id, action, payload}`. The payload
schemas are deliberately exact:

- `collect_diagnostics`: `{}`
- `collect_miner_logs`: `{}`; returns bounded, sanitized lines written in the
  fixed managed-miner runtime during the most recent 60 seconds
- `stop_mining`: `{}`; idempotently stops only the managed process whose PID,
  executable, creation time, and recorded argument fingerprint still match the Agent-owned record
- `restart_mining`: `{}`; reloads, validates, and starts the last active managed
  flight sheet after safely stopping its current process
- `remove_flight_sheet`: `{}`; stops only the verified RigPilot-owned miner,
  then removes only the configured local active-flight-sheet file. It never accepts
  a caller-provided path and rejects links/reparse points.
- `scan_mining_residue.v1`: `{}`; performs a non-destructive scan of only the
  fixed RigPilot process records and returns a secret-free SHA-256 scan
  fingerprint plus exact/ambiguous findings
- `cleanup_mining_residue.v1`: legacy Agents accept
  `{"scan_fingerprint": "<sha256>"}`. Selective Agents accept that fingerprint
  plus `targets` containing fixed roles and their scan-returned
  `target_fingerprint`, and independent `reset_srbminer_oc` /
  `reset_nvml_oc` booleans. Schema-v3 Agents can also return one
  `manual_candidates` entry for an ambiguous role after a complete scan found
  either one live same-name process at the trusted recorded executable path,
  or one Linux process group with a unique leader where every same-path
  same-name member shares that leader's SID/PGID and is its verified descendant.
  Selecting it requires the returned `candidate_fingerprint` in that target.
  The Agent immediately repeats the complete scan, requires the global,
  target, and optional candidate fingerprints to match, and rechecks
  PID/path/create-time/current-argv (and every selected group member) immediately
  before termination. It removes the unchanged record only after another
  complete scan proves the selected process group did not respawn. It never
  kills by an old PID or executable name.
- `restart_service`: `{"service": "name-from-local-allowlist"}`
- `reboot`: `{}` and local `allow_reboot` must be true
- `shutdown`: `{}` and local `allow_shutdown` must be true
- `apply_flight_sheet`: exact top-level fields are `flight_sheet_id`, `name`,
  `worker_name`, `primary`, nullable `secondary`, and optional SRBMiner
  `gpu_oc`. Each mining leg contains a public wallet, pool URL, structured
  algorithm/options, selected GPU indices, package metadata, and `miner_type`.
  `xmrig` and `srbminer` select the separately opted-in built-in manager; a
  null/absent `miner_type` keeps the legacy local-supervisor protocol. Managed
  requests still carry a compatibility profile field on the wire, but the Web
  UI does not ask for it and the Agent does not apply the legacy profile
  allowlist to managed miners.
- `apply_gpu_profile`: structured NVIDIA NVML values for power limit, core clock
  offset, locked core clock, memory clock offset, locked memory clock, and fan;
  local enablement and hard bounds are checked before vendor-driver limits
- `agent_update.v1`: exact fields are `release_id`, a strict newer `X.Y.Z`
  version, controller-computed `sha256`, byte `size`, and the command-bound
  relative download URL. It is advertised only when `allow_agent_updates=true`
  and the frozen Agent runs in its protected managed installation: the Windows
  portable root `%ProgramData%\RigPilotAgentPortable` with its installed ACL,
  or the fixed Linux root systemd installation.

Remote Agent updates use an exact manifest-bound platform archive, authenticated same-origin
streaming, size/SHA-256 verification, safe extraction, and immutable version
slots. A fixed updater from the currently trusted installation waits for the
staging result to reach the controller, switches the active pointer, and starts
the replacement without touching `data`. The replacement must complete two
authenticated heartbeats before activation is finalized. Failure rolls back the
pointer, restarts the prior Agent, and reports a terminal failure. A running
managed miner is stopped immediately before activation and its prior running state is stored
under the protected portable data root. The replacement restores the original
flight sheet after it reconnects; staging failure or rollback restores it with
the previous Agent. Legacy/unknown process records still fail closed. Trust in
this release is controller TLS + Agent authentication + the server-computed
archive SHA-256 and per-file manifest hashes; asymmetric code signing is not yet
implemented. Source and obsolete Windows installed-app Agents do not advertise this capability.
An original 0.5.6 binary cannot understand the command and therefore needs one
manual, identity-preserving bootstrap before later releases can update remotely.

Linux service and power actions use `systemctl`. Windows service actions use
`sc.exe`; power actions resolve `%SystemRoot%\System32\shutdown.exe` directly,
then request a fixed 10-second forced reboot/shutdown so the Agent can report
the accepted task before Windows exits. Every process is executed as an
argument vector with `shell=False`. A fresh Windows portable autostart configuration
enables both local power switches because its scheduled task runs as SYSTEM;
source configurations continue to default both switches off.

## Flight sheets and GPU OC

`flight_sheet_file` enables flight-sheet writes. It remains disabled when null.
For the 0.5.3+ managed workflow, `managed_mining_enabled` must be true and
`managed_miner_runtime_dir`, `miner_packages_dir`, and
`allow_miner_downloads=true` must be configured locally. Managed miners do
not depend on `miner_profile_allowlist` or an external `miner_service`.

The controller discovers verified official releases for each miner and exposes
Windows/Linux x64 availability in the flight-sheet editor. The selected version
is encoded in a deterministic built-in package ID. XMRig, SRBMiner-MULTI,
Pearl Miner and Seine use the authenticated controller package flow.

Miner package download is separately disabled by default. To enable it, set
`allow_miner_downloads` and a data-only `miner_packages_dir`. Packages uploaded
to the enrolled controller are fetched only from its fixed authenticated Agent
endpoint. Legacy external URLs remain supported, but require an exact
`miner_download_host_allowlist` entry. Package SHA-256 metadata remains optional
for legacy unbound records; whenever a digest is present, the Agent enforces it
against the downloaded bytes before extraction. External metadata uses an HTTPS URL
(plain HTTP is accepted only for loopback) and a `zip` or `tar.gz` archive type.
Agent credentials are never attached to external URLs.
A total download deadline and downloaded/expanded size limits are enforced;
redirects, path traversal, Windows ambiguous paths, links, special files, and
duplicate archive paths are rejected. Installation is an atomic directory move,
and cached content is re-hashed before reuse.

Downloaded content is never executed unless `managed_mining_enabled` is true
and `managed_miner_runtime_dir` is configured. Built-in managed legs require a package ID
in the exact `builtin-*` or isolated `test-*` namespace declared by the selected
trusted adapter. Descriptors carrying the retired `hiveos_custom` field or a
`hiveos-*` package ID are rejected explicitly.
For built-in miners, the Agent scans the verified package without
following links and accepts one exact official executable name. It allocates API ports and log/config paths locally and starts
an argument vector with `shell=False`. A previous miner is stopped only after
PID, executable path, process creation time, and recorded argument fingerprint
all match its private local record.

Native SRBMiner discovery, OC reset and mining use separate writable runs
copied from the SHA-attested package. Each run receives an exclusive directory;
starting does not overwrite an old run or require every Windows process to be
readable. Prepared leases and committed process records retain their runs.
Confirmed completion disposes only the identity-bound run; uncertain old data
and legacy slots remain untouched. Failed or interrupted cleanup is retained
for review, and insufficient actual disk space reports the required and
available bytes.

### Built-in miner behavior

Seine runs CPU-only BNT (`blocknet-argon2id`) pool jobs. Omitted threads use
automatic sizing; explicit threads must be positive and have no invented
upper limit. The optional CPU profile is balanced, throughput, or efficiency.
Its pool protocol has no password, so any nonempty password is rejected.
For Seine 0.2.23 and newer, managed launches disable the upstream GitHub
update check; for 0.2.26 and newer they also pass `--telemetry off`. Seine
0.2.45 adds `--self-update` and `--auto-update`, which are reserved and cannot
be enabled through `extra_args`; RigPilot remains the only update authority.
Users cannot override these external-network defaults through `extra_args`. The
API is private loopback `/v1/runtime/state`; 0.2.23 and newer require the
per-run bearer cookie generated in Seine's private data directory. A fresh
native STATS counter window credits work once; repeated events keep their
original timestamps and cannot earn duplicate work. Pool shares remain
unknown when the API does not report them. Seine 0.2.27 and newer applies a
flat 2% upstream developer fee everywhere. Official 0.2.44/0.2.45 Linux
assets require glibc 2.39; separately identified same-version
`test-seine-*-linux-x64` compatibility builds target Ubuntu 22.04/glibc 2.35
and are selected explicitly as uploaded packages. Their exact SHAs identify
these independent builds; they never silently replace official stable
packages. Historical 0.2.15 package behavior remains version-aware.

The simplified XMRig adapter is XMR/RandomX-only. `cpu_threads` maps to XMRig's
explicit thread count; when omitted, XMRig selects threads automatically. The
SRBMiner adapter builds one structured SRBMiner command. A supported secondary
leg is passed to that same process and must use the same miner type and package
version; it is not an arbitrary second executable. SRBMiner GPU flight sheets
may carry model rules such as `5080`, `5070`, and `5060 Ti` for core offset,
locked core clock, memory offset, locked memory clock, and power settings. Before
launch the Agent reads SRBMiner `--list-devices`, filters integrated and
algorithm-incompatible GPUs, then expands each rule to every selected card with
that model. Cards without a matching rule keep default OC values and continue
mining; rules for absent models are ignored. Ambiguous or overlapping rules fail
closed. Matched values are emitted directly as the
official `--gpu-coffset0/1`, `--gpu-cclock0/1`, `--gpu-moffset0/1`,
`--gpu-mclock0/1`, and `--gpu-plimit0/1` arguments. Algorithm slot A0 uses the
`0` suffix and A1 uses `1`; offset and lock are independent and may be supplied
together. All model rows must use the same set of controls; zero/default
placeholders keep comma-separated values aligned for unmatched cards. When local
GPU OC policy is enabled, every SRBMiner launch first asks the miner to restore
NVIDIA defaults, including a launch whose active flight sheet contains no OC.
The reset is intentionally repeated on later restarts so a failed or interrupted
earlier transition cannot leave a power/clock limit hidden behind a saved no-OC
sheet; selected flight settings are applied only after that reset.
Starting any SRBMiner GPU flight also clears a previously applied standalone
NVML profile first, including when that flight declares no OC. CPU-only
SRBMiner and XMRig flights leave an unrelated standalone GPU profile alone.
Legacy `miner_profile_allowlist`, `flight_sheet_extra_key_allowlist`, and
`miner_service` behavior remains available only when `miner_type` is absent or
null. Generic uploads use that legacy path; uploads are never treated as
built-in releases.

Agent 0.8.31 accepts `extra_config.extra_args` for XMRig, SRBMiner and
Pearl Miner as up to 4096 characters of editable text. Quotes group literal
arguments; Windows backslashes and multiline whitespace are preserved without
shell expansion. Unknown future software flags pass through. Parameters that
control generated pool/wallet/worker, devices, API, private configuration/log
paths or managed OC must use the structured flight fields. Invalid quotes,
controls or conflicting flags fail before an existing miner is stopped.
Empty text clears the arguments. Independent jobs each use their own text;
integrated SRB has one process and appends primary then secondary arguments,
with the upstream flags' process/per-algorithm meaning unchanged.
For example, SRBMiner 3.7.2 supports `--pearl-k1` or `--pearl-k1 0,2`.
This is a software kernel selector, not a replacement for managed GPU selection.
Nonempty native extra arguments require Agent 0.8.31 and
`managed_mining.extra_args.v1` before dispatch.

lolMiner and ForgeMiner are retired. Historical flight and process identities
remain readable, and exact owned old processes may be stopped or replaced using
the existing PID, creation-time and argv proofs. Their adapters, downloads,
launch handlers and telemetry are absent; old records are never converted into
another miner or restarted automatically.

On Linux, the Agent also reports a bounded CPU model in `metrics.cpu.model`
(with the same value retained as `inventory.processor`) and the already-existing
physical memory size in `metrics.memory.total_bytes`.  This avoids relying on
`platform.processor()`, which is commonly empty on Ubuntu.

New Linux system Agents may additionally advertise
`remote_access.ssh_manage.v1`. An administrator can then enable or disable the
fixed local `ssh.service` on port 22 from the device detail page. This action
cannot select a command, path, port, account, firewall rule, or package: it
installs only RigPilot's fixed private-source policy for the existing `user`
account, forbids direct root login, and never changes remote access. If
`openssh-server` is absent the command fails with local installation guidance;
the SSH action never runs a package manager. The host firewall
and router/NAT configuration remain the administrator's responsibility.

Each heartbeat includes `inventory.mining_status` when the Agent runs with its
normal configuration. A managed miner binds its API to `127.0.0.1`; the Agent
queries XMRig's `/2/summary` or SRBMiner's local API
with a short timeout and a bounded response, extracts only running state,
hashrate, and accepted/rejected
Share counts, and reports them to the controller. API errors are reported as
status errors rather than changing miner configuration. Wallet addresses, pool
passwords, executable paths, and raw API documents are omitted. A legacy local
supervisor may still atomically write the limited JSON configured by
`miner_status_file`; legacy service checks continue to use argument-vector
`sc.exe` / `systemctl` calls with `shell=False`.

SRBMiner's watchdog can replace its own process and PID while continuing to
mine. New process records therefore store a SHA-256 fingerprint of the exact
argument vector plus the loopback API port, never the arguments themselves. On
the next heartbeat—or immediately before stop/restart/apply—the Agent rebinds
only one newer process with the exact managed executable and fingerprint. A
different command line, multiple candidates, or an inaccessible same-name
process fails closed and preserves the old proof instead of starting a second
miner.

The `collect_miner_logs` command is intentionally on-demand rather than part of
heartbeat telemetry. It reads fixed built-in miner/launcher logs below
`managed_miner_runtime_dir`, selects the most recent 60 seconds,
removes terminal control sequences and common secret values, and enforces line,
tail, and JSON-size limits before returning. It does not accept a path or time
window from the controller. The Web live-log dialog repeatedly invokes this
same bounded command only while the dialog is open, de-duplicates overlapping
snapshots in the browser, and stops polling (cancelling an unclaimed command)
as soon as the dialog closes.

`allow_gpu_oc` must be true before any direct device adjustment or SRBMiner flight OC
is considered. The device-detail single-card action uses the official NVIDIA NVML driver API
on Windows and Linux and expose exactly six operations: power limit, core clock
offset, locked core clock, memory clock offset, locked memory clock, and fan
percentage. SRBMiner flight OC uses SRBMiner's own A0/A1 arguments described
above rather than the standalone NVML transition path. The Agent first
intersects values with its local bounds and then preflights NVML-reported
hardware limits for direct adjustments. Unsupported devices, missing
administrator/root permissions, and out-of-range values fail explicitly. The
source configuration defaults this switch off; a fresh 0.5.8-or-later Windows
installer/portable configuration sets it true for NVIDIA only. That convenience
is not a safety guarantee: use conservative values, monitor temperature
independently, and test one GPU first. The controller does not accept new AMD
adjustments. The controller no longer exposes a separate GPU-profile page;
it builds the same exact structured `apply_gpu_profile` payload for the selected
reported NVIDIA index.

The device list exposes `stop_mining` and `restart_mining` only to operators and
administrators for supported online Agents. Stop restores RigPilot-owned
SRBMiner and NVML OC before reporting completion. Restart uses the last active
flight sheet; both commands reject extra payload fields and are recorded like
other remote tasks. Agents with managed mining enabled also advertise
`managed_mining.residue_repair.v1` for the two-phase scan/cleanup workflow.
Agents implementing per-role target fingerprints additionally advertise
`managed_mining.residue_repair.selective.v1`; controllers must not send the
selective payload to older Agents. Agents supporting the schema-v3,
operator-selected non-exact candidate flow advertise
`managed_mining.residue_repair.explicit_candidate.v1`.

The controller and this file store public payout addresses only. Do not place a
private key, seed phrase, exchange password, or API secret in a wallet or miner
configuration.

## Tests

```text
python -m pytest -q agent/tests
```
