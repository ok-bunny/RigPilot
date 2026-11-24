"""Enrollment and heartbeat lifecycle."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests

from .client import ControllerRequestError, ProtocolError, RigPilotClient
from .agent_updates import (
    ACTIVATION_HEARTBEAT_SECONDS,
    AgentUpdateError,
    AgentUpdateManager,
    abort_staged_agent_update,
    arm_staged_agent_update,
    confirm_pending_agent_update,
    linux_pending_activation_recovery,
    linux_trigger_activation_recovery,
    mark_rollback_reported,
    mark_activation_reported,
    note_pending_agent_heartbeat,
    pending_activation_active,
    pending_miner_resume_markers,
    pending_rollback_results,
    portable_pending_activation_recovery,
    portable_trigger_activation_recovery,
    quarantine_portable_rollback_slot,
    resume_managed_miner_after_update,
    resume_pending_agent_update_miner,
    reconcile_confirmed_release_root,
)
from .commands import (
    Command, CommandExecutor, CommandOutcome, CommandRejected,
)
from .miner_cache_cleanup import SCAN_ACTION as MINER_CACHE_SCAN_ACTION
from .config import AgentConfig
from .deadline_watchdog import DeadlineWatchdog, ExpiredDeadline
from .inventory import collect_host_data
from .host_data import HostDataSampler, ONCE_HOST_DATA_PREWARM_TIMEOUT_SECONDS
from .linux_supervision import SystemdWatchdog, schedule_supervision_policy_repair
from .managed_miners import ManagedMinerError, ManagedMinerManager
from .state import (
    AgentState,
    StateError,
    clear_pending_replacement,
    load_or_create_pending_replacement,
    load_pending_replacement,
    replacement_pending_path,
    clear_pending_enrollment,
    load_pending_enrollment,
    load_or_create_pending_enrollment,
    load_state,
    save_state,
)
from .pearl_workload import PearlWorkloadCounters


LOGGER = logging.getLogger(__name__)
MAX_HEARTBEAT_FAILURE_RETRY_SECONDS = 10.0
MAX_STOP_POLL_SECONDS = 0.5
HEARTBEAT_STALL_EXIT_CODE = 70
_HEARTBEAT_MAINTENANCE_KEY = "_heartbeat_maintenance"
_MAINTENANCE_UNKNOWN = "unknown"
_MAINTENANCE_HINT_NAMES = frozenset(
    {
        "pending_agent_update",
        "rollback_receipt",
        "supervision_repair",
    }
)


def _controller_result_response_is_ambiguous(exc: BaseException) -> bool:
    """Only errors with no controller response may authorize Linux handoff."""

    if isinstance(exc, ControllerRequestError):
        return exc._network_error and not exc.response_received
    if isinstance(exc, ProtocolError):
        return False
    if isinstance(exc, requests.RequestException):
        return getattr(exc, "response", None) is None
    return False


def _rejected_command_identity(raw: Any) -> tuple[str, str] | None:
    """Return a stable UUID and fingerprint for a rejected wire command.

    The controller can otherwise redeliver an unsupported command forever.
    Only a canonical JSON object with a UUID command id is safe to acknowledge;
    legacy non-UUID ids remain ignored because they cannot name a controller
    command row reliably.
    """

    if not isinstance(raw, dict):
        return None
    raw_id = raw.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    try:
        command_id = str(uuid.UUID(raw_id))
    except (AttributeError, ValueError):
        return None
    if raw_id.casefold() != command_id:
        return None
    try:
        canonical = json.dumps(
            {key: value for key, value in raw.items() if key != "id"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return command_id, hashlib.sha256(canonical).hexdigest()


class AgentRuntime:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.client = RigPilotClient(config)
        self.update_manager = AgentUpdateManager(config, self.client)
        self.executor = CommandExecutor(
            config, agent_update_manager=self.update_manager
        )
        self.stop_event = threading.Event()
        self._signal_stop_requested = False
        self.state: AgentState | None = None
        self._workload_counters = PearlWorkloadCounters()
        self._host_data_sampler = HostDataSampler(self._collect_host_data)
        self._heartbeat_watchdog = self._create_heartbeat_watchdog()
        # PID 1 supervision is useful only when the fixed Linux drop-in is
        # active. Its helper thread is progress-gated, so a wedged main loop
        # eventually stops feeding systemd even if the process still exists.
        self._systemd_watchdog = SystemdWatchdog()
        self._allow_supervision_repair = False
        # Normal service runs reconcile fixed host policy and interrupted
        # update receipts after an authenticated heartbeat. ``--once`` is also
        # used by first-boot enrollment before Install.sh has created the final
        # systemd/unit layout; attempting those repairs there records a
        # guaranteed, but misleading, fixed_path_unsafe/active_layout_unsafe
        # failure. Keep one-shot inventory collection free of local repair and
        # update-maintenance side effects.
        self._allow_local_maintenance = True
        self._heartbeat_maintenance_hints: dict[str, bool | None] = {}
        # Fast authenticated heartbeats while this Agent runs a pending but
        # unconfirmed activation: two of them must land inside the fixed
        # updater's confirmation window even on slow mining disks.
        self._fast_activation_window = False
        # Rate limit for the Agent-side bounded activation self-recovery.
        self._last_self_recovery_attempt = 0.0
        self._systemd_watchdog.progress(
            min(1800.0, max(90.0, float(self.config.request_timeout) + 30.0))
        )

    def _create_heartbeat_watchdog(self) -> DeadlineWatchdog | None:
        """Enable fail-stop recovery only for a Linux systemd service."""

        if (
            platform.system() != "Linux"
            or not os.environ.get("INVOCATION_ID", "").strip()
        ):
            return None
        try:
            return DeadlineWatchdog(self._heartbeat_deadline_expired)
        except Exception as exc:
            # A diagnostic safety helper must never prevent enrollment or an
            # ordinary heartbeat on a resource-constrained device.
            LOGGER.error(
                "heartbeat wall-clock watchdog could not start",
                extra={
                    "event": "heartbeat_watchdog_start_failed",
                    "error_type": type(exc).__name__,
                },
            )
            return None

    @staticmethod
    def _heartbeat_deadline_expired(expired: ExpiredDeadline) -> None:
        """Exit wedged systemd Agent while preserving its managed miners."""

        # Do not log or write to stderr here.  Under systemd stderr is a
        # journald stream and a full/stalled stream can block even ``os.write``
        # forever, defeating the fail-stop callback itself.  The Linux unit
        # uses KillMode=process and Restart=on-failure, so this exits and
        # restarts only the Agent process, not the independent miner session.
        os._exit(HEARTBEAT_STALL_EXIT_CODE)

    def _arm_heartbeat_watchdog(
        self,
        phase: str = "heartbeat_http",
        timeout_seconds: float | None = None,
    ) -> tuple[DeadlineWatchdog | None, int | None]:
        watchdog = self._heartbeat_watchdog
        if watchdog is None:
            return None, None
        timeout = (
            # One heartbeat cycle may include bounded local inventory probes,
            # the controller request, and one post-update activation receipt.
            # USB-root systems need headroom for those healthy slow paths.
            max(90.0, float(self.config.request_timeout) + 30.0)
            if timeout_seconds is None
            else max(1.0, float(timeout_seconds))
        )
        try:
            return watchdog, watchdog.arm(phase, timeout)
        except Exception as exc:
            LOGGER.error(
                "heartbeat wall-clock watchdog could not be armed",
                extra={
                    "event": "heartbeat_watchdog_arm_failed",
                    "error_type": type(exc).__name__,
                },
            )
            self._heartbeat_watchdog = None
            try:
                watchdog.close()
            except Exception:
                pass
            return None, None

    @staticmethod
    def _disarm_heartbeat_watchdog(
        watchdog: DeadlineWatchdog | None,
        token: int | None,
    ) -> None:
        if watchdog is None or token is None:
            return
        try:
            watchdog.disarm(token)
        except Exception as exc:
            LOGGER.error(
                "heartbeat wall-clock watchdog could not be disarmed",
                extra={
                    "event": "heartbeat_watchdog_disarm_failed",
                    "error_type": type(exc).__name__,
                },
            )

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()
        self._host_data_sampler.request_stop()

    def _handle_signal(self, *_args: Any) -> None:
        # Python may run this handler while the interrupted thread owns an
        # internal threading.Condition lock. Event.set() acquires that same
        # kind of lock and can therefore self-deadlock. Keep the handler to one
        # ordinary assignment; the main loop polls this flag at most every
        # MAX_STOP_POLL_SECONDS and performs all cleanup outside the callback.
        self._signal_stop_requested = True

    def _stop_requested(self) -> bool:
        return self._signal_stop_requested or self.stop_event.is_set()

    def _wait_for_stop(self, delay: float) -> bool:
        """Wait interruptibly without making POSIX signal polling unbounded."""

        remaining = max(0.0, delay)
        while remaining > 0:
            if self._stop_requested():
                return True
            wait_slice = min(MAX_STOP_POLL_SECONDS, remaining)
            if self.stop_event.wait(wait_slice):
                return True
            remaining -= wait_slice
        return self._stop_requested()

    def _mark_main_loop_progress(self, ttl_seconds: float = 90.0) -> None:
        """Renew the external systemd lease after bounded local progress."""

        try:
            self._systemd_watchdog.progress(ttl_seconds)
        except (TypeError, ValueError) as exc:
            LOGGER.error(
                "systemd watchdog progress lease could not be renewed",
                extra={
                    "event": "systemd_watchdog_progress_failed",
                    "error_type": type(exc).__name__,
                },
            )

    def _merge_heartbeat_maintenance_hints(self, raw: Any) -> None:
        """Merge worker-only tri-state hints without creating disk I/O.

        A known ``True`` remains sticky across an unavailable/stale telemetry
        snapshot so an active update receipt cannot be forgotten.  ``unknown``
        never authorizes a main-thread filesystem scan; the coalescing worker
        retries it.  A later complete ``False`` observation clears the sticky
        value after the transaction's terminal marker is visible.
        """

        if not isinstance(raw, dict):
            return
        for name in _MAINTENANCE_HINT_NAMES:
            value = raw.get(name, _MAINTENANCE_UNKNOWN)
            if isinstance(value, bool):
                self._heartbeat_maintenance_hints[name] = value
            elif value == _MAINTENANCE_UNKNOWN:
                if self._heartbeat_maintenance_hints.get(name) is not True:
                    self._heartbeat_maintenance_hints[name] = None

    @staticmethod
    def _command_watchdog_lease_seconds(raw: Any) -> float:
        """Allow only known package operations a longer watchdog lease."""

        action = raw.get("action") if isinstance(raw, dict) else None
        if action == "agent_update.v1":
            return 1200.0
        if action == "apply_flight_sheet":
            # Miner-package download and process replacement can legitimately
            # take several minutes, but an already-started miner must not be
            # allowed to mask a wedged Agent for twenty minutes.
            return 420.0
        return 300.0

    def install_signal_handlers(self) -> None:
        for signal_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, signal_name, None)
            if sig is not None:
                signal.signal(sig, self._handle_signal)

    def _collect_host_data(self) -> dict[str, Any]:
        return collect_host_data(
            self.config.collect_gpu_metrics, self.config.nvidia_smi_path, self.config
        )

    def _host_data(self) -> dict[str, Any]:
        # Refresh runs on one coalescing worker.  Enrollment and heartbeats use
        # the last good JSON snapshot immediately, falling back to the minimum
        # required identity payload before the first collection completes.
        return self._host_data_sampler.snapshot()

    def _clear_saved_enrollment_pending(
        self, state: AgentState, *, state_is_durable: bool = False,
    ) -> None:
        try:
            pending = load_pending_enrollment(self.config.state_file, self.config.server_url)
            if pending is not None and pending.proposed_agent_secret == state.agent_secret:
                if not state_is_durable:
                    # A previous replace may be visible even though its final
                    # directory fsync failed. Keep the recovery journal until
                    # the primary credentials have been saved durably.
                    save_state(self.config.state_file, state, self.config.command_history_limit)
                clear_pending_enrollment(self.config.state_file)
        except StateError as exc:
            # Keep using the readable identity and preserve any journal that
            # could not be safely removed; never register a second identity.
            LOGGER.warning("pending enrollment cleanup failed", extra={
                "event": "enrollment_state_cleanup_failed", "error_type": type(exc).__name__,
            })

    def ensure_enrolled(
        self,
        cli_token: str | None = None,
        cli_replacement_token: str | None = None,
    ) -> AgentState:
        state = load_state(self.config.state_file)
        if state is not None:
            self._clear_saved_enrollment_pending(state)
            try:
                if os.path.lexists(replacement_pending_path(self.config.state_file)):
                    # A visible primary replacement may precede a failed
                    # directory fsync. Keep its retry journal until durable.
                    save_state(self.config.state_file, state, self.config.command_history_limit)
                clear_pending_replacement(self.config.state_file)
            except StateError as exc:
                LOGGER.warning(
                    "stale replacement state could not be removed",
                    extra={
                        "event": "replacement_state_cleanup_failed",
                        "error": str(exc),
                    },
                )
            self.state = state
            return state
        explicit_enrollment_token = (
            cli_token or os.environ.get("RIGPILOT_ENROLLMENT_TOKEN")
        )
        replacement_token = cli_replacement_token or os.environ.get(
            "RIGPILOT_REPLACEMENT_TOKEN"
        )
        if explicit_enrollment_token and replacement_token:
            raise RuntimeError(
                "normal enrollment and device inheritance tokens cannot be used together"
            )
        # A full installer can explicitly choose inheritance while an older
        # config still carries an ordinary enrollment token. Its explicit mode
        # wins; two explicitly supplied tokens remain an error.
        token = (
            explicit_enrollment_token
            or (None if replacement_token else self.config.enrollment_token)
        )
        pending_enrollment = load_pending_enrollment(
            self.config.state_file, self.config.server_url, token,
        )
        pending_replacement = load_pending_replacement(
            self.config.state_file, self.config.server_url, replacement_token,
        )
        if pending_enrollment is not None and pending_replacement is not None:
            raise RuntimeError(
                "ordinary enrollment and device inheritance are both pending; manual review required"
            )
        if pending_replacement is not None and not replacement_token:
            raise RuntimeError(
                "device inheritance is pending; retry with the original inheritance token"
            )
        if not token and not replacement_token and pending_enrollment is None:
            raise RuntimeError(
                "agent is not enrolled; provide an enrollment token or a device "
                "inheritance token"
            )
        if replacement_token:
            if pending_enrollment is not None:
                raise RuntimeError("ordinary enrollment is pending; resolve it before device inheritance")
            LOGGER.info(
                "inheriting existing device identity",
                extra={"event": "device_inheritance_started"},
            )
            pending = load_or_create_pending_replacement(
                self.config.state_file, self.config.server_url, replacement_token,
            )
            if load_pending_enrollment(
                self.config.state_file, self.config.server_url
            ) is not None:
                raise RuntimeError(
                    "ordinary enrollment is pending; resolve it before device inheritance"
                )
            state = self.client.replace_identity(
                replacement_token,
                self._host_data(),
                request_id=pending.request_id,
                proposed_agent_secret=pending.proposed_agent_secret,
            )
            if state.agent_secret != pending.proposed_agent_secret:
                raise ProtocolError("replacement credentials do not match the pending identity")
            save_state(
                self.config.state_file,
                state,
                self.config.command_history_limit,
            )
            clear_pending_replacement(self.config.state_file)
            self.state = state
            LOGGER.info(
                "existing device identity inherited",
                extra={
                    "event": "device_inheritance_succeeded",
                    "agent_id": state.agent_id,
                },
            )
            return state
        if pending_enrollment is None and token and not 20 <= len(token.strip()) <= 256:
            raise ProtocolError("enrollment token must contain between 20 and 256 characters")
        LOGGER.info("enrolling agent", extra={"event": "enrollment_started"})
        host = self._host_data()
        metadata = (
            RigPilotClient.enrollment_metadata(self.config, host)
            if pending_enrollment is None else pending_enrollment.metadata
        )
        # Even a readable journal may follow an interrupted directory fsync.
        # Reconfirm durable publication before every credential-bearing POST.
        pending_enrollment = load_or_create_pending_enrollment(
            self.config.state_file, self.config.server_url, token, metadata,
        )
        if load_pending_replacement(
            self.config.state_file, self.config.server_url
        ) is not None:
            raise RuntimeError(
                "device inheritance is pending; retry with the original inheritance token"
            )
        try:
            state = self.client.enroll(
                token, host, request_id=pending_enrollment.request_id,
                proposed_agent_secret=pending_enrollment.proposed_agent_secret,
                metadata=pending_enrollment.metadata,
            )
        except ControllerRequestError as exc:
            if token and exc.rejected_enrollment_request_id == pending_enrollment.request_id:
                try:
                    current = load_pending_enrollment(
                        self.config.state_file, self.config.server_url, token,
                    )
                    if current == pending_enrollment:
                        clear_pending_enrollment(self.config.state_file)
                        # Only a bound, definitive rejection with durable
                        # local cleanup may unlock an outer bootstrap journal.
                        exc.enrollment_attempt_cleared = True
                except StateError as cleanup_error:
                    LOGGER.warning("rejected enrollment cleanup failed", extra={
                        "event": "enrollment_rejection_cleanup_failed",
                        "error_type": type(cleanup_error).__name__,
                    })
            raise
        if state.agent_secret != pending_enrollment.proposed_agent_secret:
            raise ProtocolError("enrollment credentials do not match the pending identity")
        save_state(self.config.state_file, state, self.config.command_history_limit)
        self.state = state
        self._clear_saved_enrollment_pending(state, state_is_durable=True)
        LOGGER.info(
            "agent enrolled",
            extra={"event": "enrollment_succeeded", "agent_id": state.agent_id},
        )
        return state

    def _remember_outcome(
        self, state: AgentState, command: Command, outcome: CommandOutcome
    ) -> dict[str, Any]:
        api_result = outcome.to_api()
        if command.action == MINER_CACHE_SCAN_ACTION:
            # The scan has no side effects and may be requested precisely when
            # the disk is full. Keep the bounded result transient instead of
            # growing the credential-bearing state file just to cache it.
            # A lost HTTP response can safely trigger a fresh local scan.
            return api_result
        state.command_history[command.command_id] = {
            "fingerprint": command.fingerprint(),
            "response": api_result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        save_state(self.config.state_file, state, self.config.command_history_limit)
        return api_result

    def _finish_staged_activation(
        self,
        state: AgentState,
        command: Command,
        response: dict[str, Any],
        *,
        accepted_status: str | None = None,
        report_error: BaseException | None = None,
        replayed: bool = False,
    ) -> None:
        result = response.get("result")
        if (
            response.get("status") != "succeeded"
            or not isinstance(result, dict)
            or result.get("activation_scheduled") is not True
        ):
            return
        if platform.system() == "Linux":
            may_activate = (
                _controller_result_response_is_ambiguous(report_error)
                if report_error is not None
                else accepted_status == "running"
            )
            if report_error is None and not may_activate and not replayed:
                LOGGER.warning(
                    "controller did not accept staged Agent activation",
                    extra={
                        "event": "agent_update_stage_rejected",
                        "command_id": command.command_id,
                        "status": accepted_status,
                    },
                )
            try:
                if may_activate:
                    if arm_staged_agent_update(
                        self.config, state, command.command_id, response
                    ):
                        self.request_stop()
                elif isinstance(result.get("activation_id"), str):
                    reason = (
                        f"controller rejected activation result: {report_error}"
                        if report_error is not None
                        else f"controller rejected activation with status {accepted_status}"
                    )
                    abort_staged_agent_update(
                        self.config, state, command.command_id, response, reason
                    )
                    resume_managed_miner_after_update(
                        self.config, state, command.command_id, result["activation_id"]
                    )
            except AgentUpdateError as exc:
                raise OSError(str(exc)) from exc
        else:
            # Windows: the fixed v1 updater is already scheduled, so this
            # process exiting is what keeps its 75s confirmation window
            # alive.  ``running`` and the storage receipts ``queued`` /
            # ``dispatched`` all mean the staged result is durably in the
            # controller, matching the Linux arm/exit acceptance set; a
            # terminal receipt is logged and left to the updater's rollback
            # instead of silently blocking the activation path.
            accepted = report_error is not None or accepted_status in {
                "running",
                "queued",
                "dispatched",
            }
            if (
                report_error is None
                and not accepted
                and not replayed
            ):
                LOGGER.warning(
                    "controller did not accept staged Agent activation",
                    extra={
                        "event": "agent_update_stage_rejected",
                        "command_id": command.command_id,
                        "status": accepted_status,
                    },
                )
            if not replayed and accepted:
                # A cached Windows result may belong to an already restored
                # old Agent. Only a newly staged updater may ask this
                # process to exit.
                self.request_stop()

    def _post_command_response(
        self,
        state: AgentState,
        command: Command,
        response: dict[str, Any],
        *,
        replayed: bool = False,
    ) -> str:
        try:
            accepted_status = self.client.post_result(
                state, command.command_id, response
            )
        except (requests.RequestException, ProtocolError) as report_error:
            # The response is already durable. An ambiguous Linux receipt may
            # hand off to the ready updater, whose next heartbeat replays it.
            self._finish_staged_activation(
                state, command, response, report_error=report_error, replayed=replayed
            )
            raise
        self._finish_staged_activation(
            state, command, response, accepted_status=accepted_status, replayed=replayed
        )
        return accepted_status

    def _handle_command(self, state: AgentState, raw: Any) -> None:
        try:
            command = Command.parse(raw)
        except CommandRejected as exc:
            identity = _rejected_command_identity(raw)
            if identity is not None:
                command_id, fingerprint = identity
                previous = state.command_history.get(command_id)
                if (
                    previous
                    and previous.get("fingerprint") == fingerprint
                    and isinstance(previous.get("response"), dict)
                ):
                    response = previous["response"]
                    event = "rejected_command_replayed"
                else:
                    error = (
                        "command id was reused with different invalid contents"
                        if previous
                        else str(exc)
                    )
                    response = {
                        "status": "failed",
                        "result": {"rejected": True},
                        "error": error[:300],
                    }
                    state.command_history[command_id] = {
                        "fingerprint": fingerprint,
                        "response": response,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    save_state(
                        self.config.state_file,
                        state,
                        self.config.command_history_limit,
                    )
                    event = "rejected_command_recorded"
                LOGGER.warning(
                    "invalid command reported as terminal failure",
                    extra={
                        "event": event,
                        "command_id": command_id,
                        "reason": str(exc),
                    },
                )
                self.client.post_result(state, command_id, response)
                return
            LOGGER.warning(
                "invalid command ignored",
                extra={"event": "command_rejected", "reason": str(exc)},
            )
            return

        previous = state.command_history.get(command.command_id)
        if previous:
            if previous.get("fingerprint") == command.fingerprint() and isinstance(
                previous.get("response"), dict
            ):
                LOGGER.info(
                    "replaying cached command result",
                    extra={"event": "command_replayed", "command_id": command.command_id},
                )
                cached_response = previous["response"]
                accepted_status = self._post_command_response(
                    state, command, cached_response, replayed=True
                )
                return
            outcome = CommandOutcome(
                status="failed",
                result={"rejected": True},
                error="command id was reused with different contents",
            )
            api_result = self._remember_outcome(state, command, outcome)
            self.client.post_result(state, command.command_id, api_result)
            return

        LOGGER.info(
            "processing command",
            extra={
                "event": "command_started",
                "command_id": command.command_id,
                "action": command.action,
            },
        )

        try:
            running_status = self.client.post_result(
                state,
                command.command_id,
                {"status": "running", "result": {}, "error": None},
            )
        except requests.RequestException as exc:
            if (
                isinstance(exc, ControllerRequestError)
                and exc.response_received
                and type(exc.http_status) is int
                and 400 <= exc.http_status < 500
                and exc.http_status not in {408, 421, 425, 429}
            ):
                # A definitive lifecycle/auth rejection does not authorize a
                # local effect. Keep the real error for heartbeat isolation.
                raise
            # Network loss, 5xx, throttling and request timeouts leave acceptance
            # unknown. Preserve their existing best-effort execution behavior.
            LOGGER.warning(
                "could not post running status; continuing",
                extra={"event": "command_running_status_failed", "command_id": command.command_id},
            )
        else:
            if running_status in {"succeeded", "failed", "cancelled", "expired"}:
                LOGGER.info(
                    "skipping command already terminal at the Controller",
                    extra={
                        "event": "command_already_terminal",
                        "command_id": command.command_id,
                        "status": running_status,
                    },
                )
                return
        try:
            outcome = self.executor.execute(command, agent_state=state)
            if not isinstance(outcome, CommandOutcome):
                raise TypeError("command executor returned an invalid outcome")
        except Exception as exc:
            # Do not let a single unexpected local backend bug leave the
            # controller command perpetually dispatchable.  Never copy the
            # exception text into durable state or the controller response: it
            # may contain command payloads, bearer secrets, paths, or process
            # output.  Exception intentionally excludes KeyboardInterrupt and
            # SystemExit so service shutdown semantics remain intact.
            LOGGER.error(
                "unexpected local command execution failure",
                extra={
                    "event": "command_unexpected_failure",
                    "command_id": command.command_id,
                    "error_type": type(exc).__name__,
                },
            )
            outcome = CommandOutcome.unexpected_failure()
        api_result = self._remember_outcome(state, command, outcome)
        self._post_command_response(state, command, api_result)
        LOGGER.info(
            "command completed",
            extra={
                "event": "command_completed",
                "command_id": command.command_id,
                "status": outcome.status,
            },
        )

    def _reconcile_pending_agent_update(self, state: AgentState) -> None:
        # A replacement may restart mining only after it has proved an
        # authenticated controller connection.  A failed resume aborts local
        # activation confirmation, preserves the durable marker, and retries
        # on the next heartbeat instead of falsely reporting update success.
        try:
            resume_pending_agent_update_miner(self.config, state)
        except AgentUpdateError as exc:
            raise OSError(str(exc)) from exc
        activation = note_pending_agent_heartbeat(self.config, state)
        if activation is not None:
            command_id = str(activation["command_id"])
            final_response = {
                "status": "succeeded",
                "result": {
                    "activation_confirmed": True,
                    "activation_id": activation["activation_id"],
                    "version": activation["version"],
                    "health_heartbeats": 2,
                },
                "error": None,
            }
            # Commit the locally proven slot first.  This eliminates a false
            # controller success followed by rollback if the process crashes
            # between the two durable writes.  If the final POST fails, the
            # replacement stays active and retries on the next heartbeat.
            if not confirm_pending_agent_update(self.config, state, activation):
                raise OSError("could not confirm the locally active Agent update")
            accepted_status = self.client.post_result(
                state, command_id, final_response
            )
            if accepted_status in {"cancelled", "expired"}:
                # The controller command reached a terminal state before this
                # confirmation arrived (operator cancel or activation-timeout
                # sweep).  Retrying forever would keep this healthy replacement
                # in the fast heartbeat window and re-POST every cycle: record
                # the report locally and keep serving from the confirmed slot.
                LOGGER.warning(
                    "controller command became terminal before activation confirmation",
                    extra={
                        "event": "agent_update_confirm_terminal",
                        "command_id": command_id,
                        "controller_status": accepted_status,
                    },
                )
                if not mark_activation_reported(self.config, state, activation):
                    raise OSError(
                        "could not record the terminally received activation"
                    )
                return
            if accepted_status != "succeeded":
                raise ProtocolError(
                    f"controller did not accept final Agent activation: {accepted_status}"
                )
            previous = state.command_history.get(command_id, {})
            state.command_history[command_id] = {
                "fingerprint": previous.get("fingerprint", ""),
                "response": final_response,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
            save_state(
                self.config.state_file,
                state,
                self.config.command_history_limit,
            )
            try:
                # Keep the intent marker through the updater's rollback
                # window.  If the replacement or its miner dies before final
                # controller acceptance, the restored old Agent can still
                # re-establish mining from this same marker.
                resume_managed_miner_after_update(
                    self.config,
                    state,
                    command_id,
                    str(activation["activation_id"]),
                )
            except AgentUpdateError as exc:
                raise OSError(str(exc)) from exc
            if not mark_activation_reported(self.config, state, activation):
                raise OSError("could not record the reported Agent activation")

    def _update_fast_activation_window(
        self, state: AgentState, *, pending_hint: bool
    ) -> bool:
        """Keep the fast-cadence window truthful across stale hints.

        The portable layout keeps its pointer on the working volume, so it is
        probed directly.  On Linux the window is entered from the worker hint
        only, and left via one direct probe once the hint stops claiming it,
        so a stale telemetry sample can neither drop an activation nor keep
        the fast cadence forever.
        """

        if platform.system() != "Linux":
            self._fast_activation_window = pending_activation_active(
                self.config, state
            )
        elif pending_hint:
            self._fast_activation_window = True
        elif self._fast_activation_window and not pending_activation_active(
            self.config, state
        ):
            self._fast_activation_window = False
        return self._fast_activation_window

    def heartbeat_once(self, state: AgentState) -> int:
        watchdog, watchdog_token = self._arm_heartbeat_watchdog()
        try:
            # Supervise local snapshot access, the controller heartbeat, and
            # the small post-heartbeat update receipt reconciliation.  A USB
            # filesystem can wedge before the HTTP call or while reading the
            # fixed update markers; without this wider boundary the Agent
            # stays alive but stops heartbeating while its miner continues.
            # Command execution remains outside this deadline because package
            # downloads and update activation have their own transactions.
            host = self._host_data()
            inventory = host.get("inventory")
            self._workload_counters.observe(host, state)
            workload_counters = self._workload_counters.heartbeat_reports()
            if workload_counters:
                host["workload_counters"] = workload_counters
            if workload_counters and isinstance(inventory, dict):
                inventory["workload_counter_status"] = dict(
                    state.workload_counter_state.get("status", {}),
                )
            raw_hints = (
                inventory.get(_HEARTBEAT_MAINTENANCE_KEY)
                if isinstance(inventory, dict)
                else None
            )
            self._merge_heartbeat_maintenance_hints(raw_hints)
            commands = self.client.heartbeat(state, host)
            workload_acks = getattr(self.client, "workload_counter_acks", None)
            workload_ack_supported = getattr(self.client, "workload_ack_supported", None)
            if isinstance(workload_ack_supported, bool):
                self._workload_counters.note_ack_support(workload_ack_supported)
            self._workload_counters.acknowledge(workload_acks)
            if self._allow_local_maintenance:
                pending_hint = (
                    self._heartbeat_maintenance_hints.get("pending_agent_update")
                    is True
                )
                activation_window = self._update_fast_activation_window(
                    state, pending_hint=pending_hint
                )
                if (
                    platform.system() != "Linux"
                    or pending_hint
                    or activation_window
                ):
                    try:
                        self._reconcile_pending_agent_update(state)
                    except Exception as exc:
                        # The authenticated HTTP heartbeat has already updated
                        # last_seen.  Pending-update bookkeeping is maintenance
                        # and must never make the main loop classify that
                        # successful heartbeat as a network failure.
                        LOGGER.error(
                            "post-heartbeat Agent update maintenance failed",
                            extra={
                                "event": "agent_update_post_heartbeat_maintenance_failed",
                                "error_type": type(exc).__name__,
                            },
                        )
                self._maybe_self_recover_activation(state)
                try:
                    self._retry_pending_miner_resumes(state)
                except Exception as exc:
                    LOGGER.error(
                        "could not retry deferred miner resumes",
                        extra={
                            "event": "agent_update_resume_reconcile_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
        finally:
            self._disarm_heartbeat_watchdog(watchdog, watchdog_token)
        # An authenticated heartbeat has already refreshed last_seen and the
        # update receipt reconciliation above has either kept the pending
        # update guard in place or confirmed the replacement slot.  Submit the
        # fixed systemd policy before executing controller commands: a first
        # flight-sheet command that wedges after starting its miner must not
        # prevent the external supervisor from ever being installed.
        if self._allow_supervision_repair and (
            platform.system() != "Linux"
            or self._heartbeat_maintenance_hints.get("supervision_repair") is True
        ):
            # The first online update from an older Linux package may need to
            # install the external systemd watchdog itself.  Until that repair
            # completes there is no external supervisor to recover a main
            # thread parked in lstat/fsync/systemd-run, so protect this narrow
            # bootstrap window with the in-process wall-clock watchdog too.
            repair_watchdog, repair_token = self._arm_heartbeat_watchdog(
                "supervision_policy_repair",
                max(90.0, float(self.config.request_timeout) + 10.0),
            )
            try:
                self._maybe_submit_supervision_policy_repair()
            finally:
                self._disarm_heartbeat_watchdog(
                    repair_watchdog, repair_token
                )
        for raw in commands:
            if self._stop_requested():
                break
            try:
                with self._systemd_watchdog.lease(
                    self._command_watchdog_lease_seconds(raw)
                ):
                    self._handle_command(state, raw)
            except (requests.RequestException, ProtocolError) as exc:
                LOGGER.error(
                    "could not report command result",
                    extra={"event": "command_result_failed", "error": str(exc)},
                )
            except Exception as exc:
                # Command persistence, local process control and optional
                # third-party backends are all downstream of a successful
                # heartbeat.  Keep polling even if one command needs retry.
                LOGGER.error(
                    "command processing failed after authenticated heartbeat",
                    extra={
                        "event": "command_processing_failed",
                        "error_type": type(exc).__name__,
                    },
                )
        return len(commands)

    def _reconcile_update_rollbacks(self, state: AgentState) -> None:
        for marker, command_id, response in pending_rollback_results(
            self.config, state
        ):
            activation_id = response.get("result", {}).get("activation_id")
            if not isinstance(activation_id, str):
                raise OSError("Agent update rollback receipt has no activation id")
            # Best effort only: a locally stuck miner resume must never stop
            # the controller from learning that this device is back on the old
            # version.  The retained marker is retried every heartbeat by
            # _retry_pending_miner_resumes below.
            resume_error: AgentUpdateError | None = None
            try:
                resume_managed_miner_after_update(
                    self.config,
                    state,
                    command_id,
                    activation_id,
                    consume_marker=False,
                )
            except AgentUpdateError as exc:
                resume_error = exc
            accepted_status = self.client.post_result(state, command_id, response)
            if accepted_status in {"succeeded", "cancelled", "expired"}:
                # A late rollback receipt cannot change an already-terminal
                # controller command.  Consume it so the stale local receipt
                # never blocks future heartbeats forever; the warning keeps
                # the controller/local disagreement visible in Agent logs.
                LOGGER.warning(
                    "controller command was already terminal before Agent rollback report",
                    extra={
                        "event": "agent_update_rollback_stale",
                        "command_id": command_id,
                        "controller_status": accepted_status,
                    },
                )
            elif accepted_status != "failed":
                raise ProtocolError(
                    f"controller did not accept Agent rollback failure: {accepted_status}"
                )
            else:
                previous = state.command_history.get(command_id, {})
                state.command_history[command_id] = {
                    "fingerprint": previous.get("fingerprint", ""),
                    "response": response,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                save_state(
                    self.config.state_file,
                    state,
                    self.config.command_history_limit,
                )
            try:
                resume_managed_miner_after_update(
                    self.config,
                    state,
                    command_id,
                    activation_id,
                    consume_marker=True,
                )
            except AgentUpdateError as exc:
                resume_error = exc
            if resume_error is not None:
                LOGGER.error(
                    "miner resume after reported Agent rollback stays pending",
                    extra={
                        "event": "agent_update_rollback_resume_pending",
                        "command_id": command_id,
                        "error_type": type(resume_error).__name__,
                    },
                )
            mark_rollback_reported(marker, command_id)
            if platform.system() != "Linux":
                rolled_version = response.get("result", {}).get("version")
                if isinstance(rolled_version, str):
                    try:
                        quarantine_portable_rollback_slot(
                            self.config, rolled_version, activation_id
                        )
                    except OSError:
                        # Quarantine is an optimization: a same-version retry
                        # merely fails its staging preflight instead.
                        LOGGER.warning(
                            "rolled-back portable slot was not quarantined",
                            extra={
                                "event": "agent_update_slot_quarantine_failed",
                                "command_id": command_id,
                            },
                        )

    def _retry_pending_miner_resumes(self, state: AgentState) -> None:
        """Retry miner resumes whose rollback report already succeeded."""

        for command_id, activation_id in pending_miner_resume_markers(
            self.config, state
        ):
            try:
                resumed = resume_managed_miner_after_update(
                    self.config,
                    state,
                    command_id,
                    activation_id,
                    consume_marker=True,
                )
            except AgentUpdateError as exc:
                LOGGER.error(
                    "deferred miner resume after Agent update is still failing",
                    extra={
                        "event": "agent_update_resume_retry_failed",
                        "command_id": command_id,
                        "error_type": type(exc).__name__,
                    },
                )
                continue
            if resumed:
                LOGGER.info(
                    "deferred miner resume after Agent update completed",
                    extra={
                        "event": "agent_update_resume_retry_completed",
                        "command_id": command_id,
                    },
                )

    def _maybe_self_recover_activation(self, state: AgentState) -> None:
        """Bound an unfinished activation when its trusted updater died.

        Primary recovery stays with the fixed updater and (on Linux) the
        recovery timer.  This fallback only runs once the updater's own
        durable deadline plus grace has provably expired without a reported
        result, which is exactly the state that otherwise leaves a device
        offline after reboot or a killed updater unit.
        """

        now = time.monotonic()
        if now - self._last_self_recovery_attempt < 60.0:
            return
        self._last_self_recovery_attempt = now
        if platform.system() == "Linux":
            recovery = linux_pending_activation_recovery(self.config, state)
            if recovery is None:
                return
            LOGGER.error(
                "pending Agent activation exceeded its updater deadline; "
                "handing it to the fixed recovery helper",
                extra={
                    "event": "agent_update_self_recovery_triggered",
                    "activation_id": recovery["activation_id"],
                },
            )
            linux_trigger_activation_recovery(recovery)
            return
        recovery = portable_pending_activation_recovery(self.config, state)
        if recovery is None:
            return
        LOGGER.error(
            "pending portable Agent activation exceeded its updater deadline; "
            "rolling back to the previous slot",
            extra={
                "event": "agent_update_self_recovery_triggered",
                "activation_id": recovery["activation_id"],
            },
        )
        portable_trigger_activation_recovery(recovery)
        self.request_stop()

    def _maybe_submit_supervision_policy_repair(self) -> None:
        try:
            if schedule_supervision_policy_repair(self.config):
                LOGGER.warning(
                    "submitted fixed Linux Agent supervision policy repair",
                    extra={"event": "agent_supervision_policy_repair_submitted"},
                )
        except Exception as exc:
            # Repair is bounded and rate-limited by linux_supervision. A local
            # policy error must stay visible without blocking normal heartbeats.
            LOGGER.error(
                "could not repair Linux Agent supervision policy",
                extra={
                    "event": "agent_supervision_policy_repair_failed",
                    "error_type": type(exc).__name__,
                },
            )

    def _start_optional_services(self, state: AgentState) -> None:
        return None

    def _recover_interrupted_flight_transition(self) -> None:
        if (
            not self.config.managed_mining_enabled
            or self.config.managed_miner_runtime_dir is None
        ):
            return
        try:
            result = ManagedMinerManager(
                self.config
            ).recover_interrupted_flight_transition()
        except ManagedMinerError as exc:
            # Preserve the journal for the next supervised retry.  Normal
            # heartbeat connectivity stays available so the controller can
            # expose the recovery error instead of making the Agent vanish.
            LOGGER.error(
                "interrupted flight transition recovery failed",
                extra={
                    "event": "flight_transition_recovery_failed",
                    "reason": str(exc),
                },
            )
            return
        if result.get("recovered") is True:
            LOGGER.warning(
                "recovered interrupted flight transition",
                extra={
                    "event": "flight_transition_recovered",
                    "reason": result.get("reason"),
                },
            )

    def _recover_pending_secondary_start(self) -> None:
        """Resume a delayed secondary-leg start that outlived a restart."""

        if (
            not self.config.managed_mining_enabled
            or self.config.managed_miner_runtime_dir is None
        ):
            return
        try:
            result = ManagedMinerManager(
                self.config
            ).recover_pending_secondary_start()
        except ManagedMinerError as exc:
            # Keep the durable marker: the next supervised startup retries the
            # same bounded launch decision instead of guessing.
            LOGGER.error(
                "pending delayed secondary start recovery failed",
                extra={
                    "event": "secondary_start_recovery_failed",
                    "reason": str(exc),
                },
            )
            return
        if result.get("recovered") is True:
            LOGGER.warning(
                "recovered pending delayed secondary start",
                extra={
                    "event": "secondary_start_recovered",
                    "reason": result.get("reason"),
                },
            )

    def _stop_optional_services(self) -> None:
        services = (
            ("host_data", self._host_data_sampler.close),
        )
        for name, stop in services:
            try:
                stop()
            except Exception as exc:
                LOGGER.error(
                    "optional Agent service could not stop cleanly",
                    extra={
                        "event": "optional_agent_service_stop_failed",
                        "service": name,
                        "error_type": type(exc).__name__,
                    },
                )

    def run(
        self,
        cli_token: str | None = None,
        once: bool = False,
        *,
        cli_replacement_token: str | None = None,
    ) -> None:
        self.install_signal_handlers()
        self._allow_supervision_repair = not once
        self._allow_local_maintenance = not once
        try:
            self._mark_main_loop_progress(
                min(1800.0, max(90.0, float(self.config.request_timeout) + 30.0))
            )
            if once:
                # Installers and frozen-binary smoke checks use ``--once`` and
                # expect one useful inventory sample.  Preserve that behavior
                # with a strict local-probe upper bound.  Do not cap this with
                # the HTTP request timeout: on Windows, the CPU-temperature
                # helper and two allowlisted WMI fallbacks alone may consume
                # twelve seconds.  A wedged local probe still cannot prevent
                # enrollment or its single heartbeat indefinitely.
                self._host_data_sampler.wait_for_first(
                    ONCE_HOST_DATA_PREWARM_TIMEOUT_SECONDS
                )
            state = self.ensure_enrolled(cli_token, cli_replacement_token)
            self._mark_main_loop_progress()
            if not once:
                self._mark_main_loop_progress(300.0)
                # Bound an activation that was interrupted by a reboot or a
                # dead updater before the first heartbeat: this is the only
                # cross-restart fallback that runs before any network I/O.
                try:
                    self._maybe_self_recover_activation(state)
                except Exception as exc:
                    LOGGER.error(
                        "startup activation self-recovery check failed",
                        extra={
                            "event": "agent_update_self_recovery_check_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                self._recover_interrupted_flight_transition()
                self._recover_pending_secondary_start()
                # Start the coalescing telemetry worker before the first loop.
                # Its private maintenance hint will request supervision repair
                # after a successful heartbeat.  Do not synchronously inspect
                # systemd/update files here: on a degraded boot disk that would
                # prevent the Agent from ever reporting liveness.
                self._host_data_sampler.request_refresh()
                self._start_optional_services(state)
                self._mark_main_loop_progress()
            interval = state.heartbeat_interval or self.config.heartbeat_interval
            failures = 0
            while not self._stop_requested():
                self._mark_main_loop_progress(
                    min(1800.0, max(90.0, float(self.config.request_timeout) + 30.0))
                )
                try:
                    count = self.heartbeat_once(state)
                except Exception as exc:
                    failures += 1
                    LOGGER.error(
                        "heartbeat failed",
                        extra={
                            "event": "heartbeat_failed",
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                            "attempt": failures,
                        },
                    )
                    if once:
                        # Installers and release smoke checks rely on the exit
                        # status to prove this authenticated heartbeat succeeded.
                        # Keep the normal service retry loop below, but surface a
                        # failed single attempt through the CLI's nonzero exit.
                        raise
                    # A handled controller/DNS/TLS failure proves the retry
                    # loop is alive; it must not cause a systemd restart storm.
                    self._mark_main_loop_progress()
                else:
                    # Success means the controller accepted the authenticated
                    # HTTP heartbeat.  Everything below is isolated maintenance
                    # and cannot increase network-failure backoff.
                    failures = 0
                    maintenance_watchdog, maintenance_token = (
                        self._arm_heartbeat_watchdog(
                            "post_heartbeat_maintenance",
                            # Policy repair can legitimately wait for multiple
                            # bounded systemd/update probes.  Keep enough headroom
                            # for that slow-but-healthy path while still turning a
                            # permanently wedged USB filesystem into a supervised
                            # Agent restart.
                            max(90.0, float(self.config.request_timeout) + 10.0),
                        )
                    )
                    self._mark_main_loop_progress(300.0)
                    try:
                        # File logging is intentionally inside this lease.  On
                        # USB-root systems even a successful heartbeat can be
                        # followed by a permanently blocked journal/JSONL write.
                        LOGGER.info(
                            "heartbeat completed",
                            extra={"event": "heartbeat_succeeded", "commands": count},
                        )
                        if self._allow_local_maintenance and (
                            platform.system() != "Linux"
                            or self._heartbeat_maintenance_hints.get(
                                "rollback_receipt"
                            )
                            is True
                        ):
                            try:
                                self._reconcile_update_rollbacks(state)
                            except Exception as exc:
                                LOGGER.error(
                                    "could not reconcile Agent update rollbacks",
                                    extra={
                                        "event": "agent_update_rollback_reconcile_failed",
                                        "error_type": type(exc).__name__,
                                    },
                                )
                        if self._allow_local_maintenance:
                            try:
                                # The confirmed slot is already active. Synchronize its
                                # root program view only after the authenticated heartbeat
                                # so slow disk/antivirus scanning cannot delay this cycle's
                                # online status or command delivery.
                                synchronized = reconcile_confirmed_release_root(
                                    self.config, state
                                )
                                if synchronized:
                                    LOGGER.info(
                                        "confirmed Agent release synchronized to the protected root",
                                        extra={"event": "agent_update_root_synchronized"},
                                    )
                            except Exception as exc:
                                LOGGER.error(
                                    "could not synchronize confirmed Agent release",
                                    extra={
                                        "event": "agent_update_root_sync_failed",
                                        "error_type": type(exc).__name__,
                                    },
                                )
                    finally:
                        self._disarm_heartbeat_watchdog(
                            maintenance_watchdog, maintenance_token
                        )
                        self._mark_main_loop_progress()
                if once:
                    return
                delay = (
                    interval
                    if failures == 0
                    else min(interval, MAX_HEARTBEAT_FAILURE_RETRY_SECONDS)
                )
                if self._fast_activation_window:
                    # Two authenticated heartbeats must confirm the running
                    # replacement well inside the fixed updater's rollback
                    # window; the normal cadence resumes once reported.
                    delay = min(delay, ACTIVATION_HEARTBEAT_SECONDS)
                self._mark_main_loop_progress(max(90.0, float(delay) + 30.0))
                # This is an in-memory Event wait split into short slices; it
                # performs no filesystem or network I/O.  Do not arm a wall-clock
                # lease here because host suspend/resume could otherwise create a
                # false stall and restart an otherwise healthy Agent.
                self._wait_for_stop(delay)
        finally:
            self._stop_optional_services()
            watchdog = self._heartbeat_watchdog
            self._heartbeat_watchdog = None
            if watchdog is not None:
                try:
                    watchdog.close()
                except Exception as exc:
                    LOGGER.error(
                        "heartbeat wall-clock watchdog could not stop cleanly",
                        extra={
                            "event": "heartbeat_watchdog_stop_failed",
                            "error_type": type(exc).__name__,
                        },
                    )
            self._systemd_watchdog.close()
