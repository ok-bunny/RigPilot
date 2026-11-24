from __future__ import annotations

from agent.version import __version__

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.commands import Command, CommandExecutor, CommandOutcome, CommandRejected
from agent.config import AgentConfig
from agent.state import AgentState


def config(**overrides):
    values = {
        "server_url": "https://example.test",
        "state_file": Path("state.json"),
    }
    values.update(overrides)
    return AgentConfig(**values)


class CommandAllowlistTests(unittest.TestCase):
    def test_unexpected_failure_outcome_is_terminal_short_and_non_diagnostic(self):
        outcome = CommandOutcome.unexpected_failure()

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.result, {"unexpected": True})
        self.assertLessEqual(len(outcome.error or ""), 300)
        self.assertNotIn("secret", outcome.error or "")

    def test_unknown_action_is_rejected_during_parse(self):
        with self.assertRaises(CommandRejected):
            Command.parse({"id": "1", "action": "shell", "payload": {"cmd": "id"}})

    def test_agent_update_is_locally_opted_in_and_calls_only_update_manager(self):
        command_id = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        payload = {
            "release_id": "12345678-1234-4123-8123-123456789abc",
            "version": ".".join([*__version__.split(".")[:2], str(int(__version__.split(".")[2]) + 1)]),
            "sha256": "a" * 64,
            "size": 100,
            "download_url": f"/api/agent/updates/{command_id}/download",
        }
        command = Command.parse(
            {"id": command_id, "action": "agent_update.v1", "payload": payload}
        )
        disabled = CommandExecutor(config()).execute(command)
        self.assertEqual("failed", disabled.status)
        self.assertIn("disabled", disabled.error or "")

        calls = []

        class Manager:
            def stage(self, value, state, received_command_id):
                calls.append((value, state, received_command_id))
                return {"activation_scheduled": True, "state": "staged"}

        state = AgentState.new(
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "secret"
        )
        enabled = CommandExecutor(
            config(allow_agent_updates=True), agent_update_manager=Manager()
        ).execute(command, agent_state=state)
        self.assertEqual("succeeded", enabled.status)
        self.assertTrue(enabled.result["activation_scheduled"])
        self.assertEqual([(payload, state, command_id)], calls)

    def test_restart_service_must_be_locally_allowlisted(self):
        executor = CommandExecutor(config(service_allowlist=frozenset({"docker"})))
        command = Command.parse(
            {"id": "2", "action": "restart_service", "payload": {"service": "sshd"}}
        )
        outcome = executor.execute(command)
        self.assertEqual("failed", outcome.status)
        self.assertTrue(outcome.result["rejected"])
        self.assertIn("allowlist", outcome.error or "")

    def test_reboot_and_shutdown_are_disabled_by_default(self):
        executor = CommandExecutor(config())
        for action in ("reboot", "shutdown"):
            with self.subTest(action=action):
                outcome = executor.execute(
                    Command.parse({"id": action, "action": action, "payload": {}})
                )
                self.assertEqual("failed", outcome.status)
                self.assertTrue(outcome.result["rejected"])

    def test_payload_does_not_accept_extra_fields(self):
        executor = CommandExecutor(config(allow_reboot=True))
        outcome = executor.execute(
            Command.parse(
                {"id": "3", "action": "reboot", "payload": {"delay": 10}}
            )
        )
        self.assertEqual("failed", outcome.status)
        self.assertTrue(outcome.result["rejected"])

    def test_windows_power_actions_use_system32_and_delay_before_forced_action(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            system_root = Path(directory)
            system32 = system_root / "System32"
            system32.mkdir()
            shutdown = system32 / "shutdown.exe"
            shutdown.write_bytes(b"placeholder")
            with (
                mock.patch("agent.commands.platform.system", return_value="Windows"),
                mock.patch.dict(os.environ, {"SystemRoot": str(system_root)}),
            ):
                executor = CommandExecutor(
                    config(allow_reboot=True, allow_shutdown=True), runner=runner
                )
                rebooted = executor.execute(Command("reboot", "reboot", {}))
                powered_off = executor.execute(Command("shutdown", "shutdown", {}))

        self.assertEqual("succeeded", rebooted.status)
        self.assertEqual("succeeded", powered_off.status)
        self.assertEqual(10, rebooted.result["delay_seconds"])
        self.assertTrue(rebooted.result["scheduled"])
        self.assertEqual("reboot", rebooted.result["action"])
        self.assertEqual("shutdown", powered_off.result["action"])
        self.assertEqual(
            [
                str(shutdown),
                "/r",
                "/f",
                "/t",
                "10",
                "/c",
                "RigPilot remote power action",
            ],
            calls[0][0],
        )
        self.assertEqual("/s", calls[1][0][1])
        for _argv, kwargs in calls:
            self.assertFalse(kwargs["shell"])
            self.assertEqual(60, kwargs["timeout"])

    def test_linux_power_actions_are_delayed_to_allow_durable_result_persistence(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            mock.patch("agent.commands.platform.system", return_value="Linux"),
            mock.patch(
                "agent.commands._linux_trusted_system_executable",
                return_value=True,
            ),
            mock.patch("agent.commands.os.getpid", return_value=4321),
        ):
            executor = CommandExecutor(
                config(allow_reboot=True, allow_shutdown=True), runner=runner
            )
            rebooted = executor.execute(Command("reboot", "reboot", {}))
            powered_off = executor.execute(Command("shutdown", "shutdown", {}))

        self.assertEqual("succeeded", rebooted.status)
        self.assertEqual("succeeded", powered_off.status)
        self.assertEqual(10, rebooted.result["delay_seconds"])
        self.assertTrue(rebooted.result["scheduled"])
        self.assertEqual("reboot", rebooted.result["action"])
        self.assertEqual("shutdown", powered_off.result["action"])
        self.assertEqual(
            [
                str(Path("/usr/bin/systemd-run")),
                "--unit",
                "rigpilot-host-reboot-4321",
                "--collect",
                "--quiet",
                "--no-block",
                "--on-active=10s",
                "--property=Type=oneshot",
                "--property=User=root",
                "--property=Group=root",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectSystem=strict",
                str(Path("/usr/bin/systemctl")),
                "reboot",
            ],
            calls[0][0],
        )
        self.assertEqual("rigpilot-host-shutdown-4321", calls[1][0][2])
        self.assertEqual("poweroff", calls[1][0][-1])
        for _argv, kwargs in calls:
            self.assertFalse(kwargs["shell"])
            self.assertEqual(60, kwargs["timeout"])

    def test_linux_power_action_fails_closed_without_trusted_systemd_tools(self):
        calls = []
        with (
            mock.patch("agent.commands.platform.system", return_value="Linux"),
            mock.patch(
                "agent.commands._linux_trusted_system_executable",
                return_value=False,
            ),
        ):
            outcome = CommandExecutor(
                config(allow_reboot=True),
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            ).execute(Command("reboot", "reboot", {}))

        self.assertEqual("failed", outcome.status)
        self.assertIn("trusted Linux systemd power tools", outcome.error or "")
        self.assertEqual([], calls)

    def test_windows_power_action_fails_closed_without_system32_binary(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory, (
            mock.patch("agent.commands.platform.system", return_value="Windows")
        ), mock.patch.dict(os.environ, {"SystemRoot": directory}, clear=True):
            executor = CommandExecutor(
                config(allow_reboot=True),
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
            outcome = executor.execute(Command("reboot", "reboot", {}))

        self.assertEqual("failed", outcome.status)
        self.assertIn("Windows system binary is unavailable", outcome.error or "")
        self.assertEqual([], calls)

    def test_dry_run_authorized_service_never_starts_process(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0, "", "")

        executor = CommandExecutor(
            config(dry_run=True, service_allowlist=frozenset({"docker"})), runner=runner
        )
        outcome = executor.execute(
            Command.parse(
                {"id": "4", "action": "restart_service", "payload": {"service": "docker"}}
            )
        )
        self.assertEqual("succeeded", outcome.status)
        self.assertTrue(outcome.result["dry_run"])
        self.assertEqual([], calls)

    def test_service_allowlist_is_exact_not_prefix_based(self):
        executor = CommandExecutor(
            config(dry_run=True, service_allowlist=frozenset({"rig-worker"}))
        )
        outcome = executor.execute(
            Command.parse(
                {
                    "id": "5",
                    "action": "restart_service",
                    "payload": {"service": "rig-worker-extra"},
                }
            )
        )
        self.assertEqual("failed", outcome.status)

    def test_mining_control_actions_require_empty_payload_and_call_manager(self):
        calls = []

        class Manager:
            def stop(self):
                calls.append("stop")
                return {"stopped": True}

            def restart(self):
                calls.append("restart")
                return {"restarted": True}

        executor = CommandExecutor(
            config(), managed_miner_factory=lambda agent_config: Manager()
        )
        stopped = executor.execute(Command("stop", "stop_mining", {}))
        restarted = executor.execute(Command("restart", "restart_mining", {}))
        rejected = executor.execute(
            Command("extra", "stop_mining", {"force": True})
        )

        self.assertEqual("succeeded", stopped.status)
        self.assertTrue(stopped.result["stopped"])
        self.assertEqual("succeeded", restarted.status)
        self.assertTrue(restarted.result["restarted"])
        self.assertEqual("failed", rejected.status)
        self.assertTrue(rejected.result["rejected"])
        self.assertEqual(["stop", "restart"], calls)

    def _nvidia_update_outcome(
        self,
        *,
        running,
        schedule,
        restart,
        partial_dual=False,
    ):
        events = []

        class Manager:
            def running(self):
                if partial_dual:
                    raise RuntimeError("independent mining is incomplete")
                return running

            def transition_has_live_processes(self):
                events.append("running")
                return running is not None

            def stop(self):
                events.append("stop")
                return {"stopped": True}

            def restart(self):
                events.append("restart")
                return restart()

        def prepare(*_args, **_kwargs):
            events.append("prepare")
            return (
                {"root": Path("program"), "state_root": Path("data")},
                {"plan_fingerprint": "a" * 64},
            )

        def schedule_worker(*_args, **_kwargs):
            events.append("schedule")
            return schedule()

        executor = CommandExecutor(
            config(allow_nvidia_driver_updates=True),
            managed_miner_factory=lambda _agent_config: Manager(),
        )
        with (
            mock.patch(
                "agent.commands.nvidia_driver_management_readiness",
                return_value={"update_available": True},
            ),
            mock.patch(
                "agent.commands.prepare_nvidia_driver_update",
                side_effect=prepare,
            ),
            mock.patch(
                "agent.commands.schedule_prepared_nvidia_driver_update",
                side_effect=schedule_worker,
            ),
            mock.patch.object(
                executor,
                "_reset_standalone_gpu_oc",
                return_value={"state_cleared": True},
            ),
        ):
            outcome = executor.execute(
                Command(
                    "driver-update",
                    "nvidia_driver.update.v1",
                    {"plan_fingerprint": "a" * 64},
                )
            )
        return outcome, events

    def test_residue_scan_and_cleanup_use_two_phase_fingerprint_contract(self):
        digest = "a" * 64
        calls = []

        class Manager:
            def scan_residue(self):
                calls.append(("scan", None))
                return {
                    "schema_version": 1,
                    "scan_fingerprint": digest,
                    "cleanup_available": True,
                    "clearable_count": 1,
                    "ambiguous_count": 0,
                    "findings": [],
                }

            def cleanup_residue(self, scan_fingerprint):
                calls.append(("cleanup", scan_fingerprint))
                return {
                    "schema_version": 1,
                    "scan_fingerprint": scan_fingerprint,
                    "cleanup_available": True,
                    "clearable_count": 1,
                    "ambiguous_count": 0,
                    "findings": [],
                    "blocked": False,
                    "changed": True,
                    "processes_stopped": 1,
                    "records_removed": 1,
                    "results": [],
                }

        executor = CommandExecutor(
            config(), managed_miner_factory=lambda _agent_config: Manager()
        )
        with mock.patch.object(
            executor,
            "_reset_standalone_gpu_oc",
            return_value={"state_cleared": True},
        ):
            scanned = executor.execute(
                Command.parse(
                    {
                        "id": "scan",
                        "action": "scan_mining_residue.v1",
                        "payload": {},
                    }
                )
            )
            cleaned = executor.execute(
                Command.parse(
                    {
                        "id": "cleanup",
                        "action": "cleanup_mining_residue.v1",
                        "payload": {"scan_fingerprint": digest},
                    }
                )
            )

        self.assertEqual("succeeded", scanned.status)
        self.assertEqual(digest, scanned.result["scan_fingerprint"])
        self.assertEqual("succeeded", cleaned.status)
        self.assertEqual(
            {"required": False, "cleared": False},
            cleaned.result["gpu_oc_reset"]["srbminer"],
        )
        self.assertTrue(
            cleaned.result["gpu_oc_reset"]["nvml"]["state_cleared"]
        )
        self.assertEqual([("scan", None), ("cleanup", digest)], calls)

        bad_digest = executor.execute(
            Command(
                "bad",
                "cleanup_mining_residue.v1",
                {"scan_fingerprint": "A" * 64},
            )
        )
        extra_scan = executor.execute(
            Command("extra", "scan_mining_residue.v1", {"force": True})
        )
        self.assertEqual("failed", bad_digest.status)
        self.assertTrue(bad_digest.result["rejected"])
        self.assertEqual("failed", extra_scan.status)
        self.assertTrue(extra_scan.result["rejected"])

    def test_residue_cleanup_preserves_structured_blocked_result(self):
        digest = "b" * 64

        class Manager:
            def cleanup_residue(self, _scan_fingerprint):
                return {
                    "schema_version": 1,
                    "scan_fingerprint": digest,
                    "cleanup_available": False,
                    "clearable_count": 0,
                    "ambiguous_count": 1,
                    "findings": [{"status": "ambiguous"}],
                    "blocked": True,
                    "changed": False,
                    "processes_stopped": 0,
                    "records_removed": 0,
                    "results": [],
                }

        outcome = CommandExecutor(
            config(), managed_miner_factory=lambda _agent_config: Manager()
        ).execute(
            Command(
                "blocked",
                "cleanup_mining_residue.v1",
                {"scan_fingerprint": digest},
            )
        )

        self.assertEqual("failed", outcome.status)
        self.assertTrue(outcome.result["blocked"])
        self.assertEqual(1, outcome.result["ambiguous_count"])
        self.assertNotIn("gpu_oc_reset", outcome.result)

    def test_empty_residue_cleanup_still_resets_srbminer_and_nvml_oc(self):
        digest = "c" * 64
        calls = []

        class Manager:
            def cleanup_residue(self, scan_fingerprint):
                calls.append(("cleanup", scan_fingerprint))
                return {
                    "schema_version": 1,
                    "scan_fingerprint": scan_fingerprint,
                    "cleanup_available": True,
                    "clearable_count": 0,
                    "ambiguous_count": 0,
                    "findings": [],
                    "blocked": False,
                    "changed": False,
                    "processes_stopped": 0,
                    "records_removed": 0,
                    "results": [],
                }

            def clear_active_srb_gpu_oc(self):
                calls.append(("srb-reset", None))
                return {
                    "required": True,
                    "cleared": True,
                    "method": "srbminer_cli_reset",
                }

        executor = CommandExecutor(
            config(), managed_miner_factory=lambda _agent_config: Manager()
        )
        with (
            mock.patch(
                "agent.commands._active_flight_uses_srb_gpu_oc",
                return_value=True,
            ),
            mock.patch.object(
                executor,
                "_reset_standalone_gpu_oc",
                return_value={"state_cleared": True},
            ),
        ):
            outcome = executor.execute(
                Command(
                    "cleanup-oc-only",
                    "cleanup_mining_residue.v1",
                    {"scan_fingerprint": digest},
                )
            )

        self.assertEqual("succeeded", outcome.status)
        self.assertTrue(
            outcome.result["gpu_oc_reset"]["srbminer"]["cleared"]
        )
        self.assertTrue(outcome.result["gpu_oc_reset"]["nvml"]["state_cleared"])
        self.assertEqual(
            [("cleanup", digest), ("srb-reset", None)],
            calls,
        )

    def test_selective_residue_cleanup_passes_targets_and_resets_only_selected_oc(
        self,
    ):
        scan_digest = "d" * 64
        target_digest = "e" * 64
        calls = []

        class Manager:
            def cleanup_residue(self, fingerprint, targets):
                calls.append(("cleanup", fingerprint, targets))
                return {
                    "schema_version": 2,
                    "scan_fingerprint": fingerprint,
                    "cleanup_available": True,
                    "clearable_count": 1,
                    "ambiguous_count": 1,
                    "findings": [],
                    "blocked": False,
                    "changed": True,
                    "processes_stopped": 1,
                    "records_removed": 1,
                    "results": [],
                    "selected_targets": targets,
                }

            def clear_active_srb_gpu_oc(self):
                raise AssertionError("SRBMiner OC was not selected")

        executor = CommandExecutor(
            config(), managed_miner_factory=lambda _agent_config: Manager()
        )
        with (
            mock.patch(
                "agent.commands._active_flight_uses_srb_gpu_oc",
                return_value=True,
            ),
            mock.patch.object(
                executor,
                "_reset_standalone_gpu_oc",
                return_value={"state_cleared": True},
            ) as reset_nvml,
        ):
            outcome = executor.execute(
                Command(
                    "selective",
                    "cleanup_mining_residue.v1",
                    {
                        "scan_fingerprint": scan_digest,
                        "targets": [
                            {
                                "role": "primary",
                                "target_fingerprint": target_digest,
                            }
                        ],
                        "reset_srbminer_oc": False,
                        "reset_nvml_oc": True,
                    },
                )
            )

        self.assertEqual("succeeded", outcome.status)
        self.assertEqual(
            {
                "srbminer": False,
                "nvml": True,
            },
            outcome.result["reset_options"],
        )
        self.assertEqual(
            "not_selected",
            outcome.result["gpu_oc_reset"]["srbminer"]["status"],
        )
        reset_nvml.assert_called_once_with()
        self.assertEqual(
            [
                (
                    "cleanup",
                    scan_digest,
                    [
                        {
                            "role": "primary",
                            "target_fingerprint": target_digest,
                        }
                    ],
                )
            ],
            calls,
        )

        rejected = executor.execute(
            Command(
                "nothing-selected",
                "cleanup_mining_residue.v1",
                {
                    "scan_fingerprint": scan_digest,
                    "targets": [],
                    "reset_srbminer_oc": False,
                    "reset_nvml_oc": False,
                },
            )
        )
        self.assertEqual("failed", rejected.status)
        self.assertTrue(rejected.result["rejected"])

    def test_residue_cleanup_passes_explicit_candidate_fingerprint(self):
        scan_digest = "a" * 64
        target_digest = "b" * 64
        candidate_digest = "c" * 64
        calls = []

        class Manager:
            def cleanup_residue(self, fingerprint, targets):
                calls.append((fingerprint, targets))
                return {
                    "schema_version": 3,
                    "scan_fingerprint": fingerprint,
                    "cleanup_available": True,
                    "clearable_count": 0,
                    "ambiguous_count": 1,
                    "findings": [],
                    "blocked": False,
                    "changed": True,
                    "processes_stopped": 1,
                    "records_removed": 1,
                    "results": [],
                    "selected_targets": targets,
                }

        executor = CommandExecutor(
            config(), managed_miner_factory=lambda _agent_config: Manager()
        )
        outcome = executor.execute(
            Command(
                "explicit-candidate",
                "cleanup_mining_residue.v1",
                {
                    "scan_fingerprint": scan_digest,
                    "targets": [
                        {
                            "role": "legacy",
                            "target_fingerprint": target_digest,
                            "candidate_fingerprint": candidate_digest,
                        }
                    ],
                    "reset_srbminer_oc": False,
                    "reset_nvml_oc": False,
                },
            )
        )

        self.assertEqual("succeeded", outcome.status)
        self.assertEqual(
            [
                (
                    scan_digest,
                    [
                        {
                            "role": "legacy",
                            "target_fingerprint": target_digest,
                            "candidate_fingerprint": candidate_digest,
                        }
                    ],
                )
            ],
            calls,
        )

        rejected = executor.execute(
            Command(
                "bad-explicit-candidate",
                "cleanup_mining_residue.v1",
                {
                    "scan_fingerprint": scan_digest,
                    "targets": [
                        {
                            "role": "legacy",
                            "target_fingerprint": target_digest,
                            "candidate_fingerprint": "not-a-digest",
                        }
                    ],
                    "reset_srbminer_oc": False,
                    "reset_nvml_oc": False,
                },
            )
        )
        self.assertEqual("failed", rejected.status)
        self.assertTrue(rejected.result["rejected"])


if __name__ == "__main__":
    unittest.main()


def test_stop_and_remove_keep_metadata_failure_separate_from_nvml_cleanup():
    for action in ("stop_mining", "remove_flight_sheet"):
        for metadata_failed in (False, True):
            events = []

            class Manager:
                def stop(self):
                    events.append("stop")
                    return {"stopped": True}

                def clear_active_srb_gpu_oc(self, *, restart_on_failure=True):
                    assert restart_on_failure is False
                    events.append("clear-srb")
                    return {"required": True, "cleared": True}

                def remove_flight_sheet(self):
                    events.append("remove")
                    return {"configuration_cleared": True}

            executor = CommandExecutor(
                config(), managed_miner_factory=lambda _config: Manager()
            )

            def reset_nvml():
                events.append("reset-nvml")
                return {"state_cleared": True}

            with (
                mock.patch(
                    "agent.commands._active_flight_uses_srb_gpu_oc",
                    side_effect=RuntimeError("metadata unreadable") if metadata_failed else None,
                    return_value=True,
                ),
                mock.patch.object(executor, "_reset_standalone_gpu_oc", side_effect=reset_nvml),
            ):
                outcome = executor.execute(Command(action, action, {}))
            expected = ["stop"]
            if not metadata_failed:
                expected.append("clear-srb")
            if action == "remove_flight_sheet":
                expected.append("remove")
            expected.append("reset-nvml")
            assert events == expected
            assert outcome.result["stopped"] is True
            assert outcome.result["gpu_oc_reset"]["state_cleared"] is True
            assert outcome.status == ("failed" if metadata_failed else "succeeded")
            if metadata_failed:
                assert outcome.result["srb_gpu_oc_reset"]["cleared"] is False
                assert "metadata unreadable" in outcome.error


def test_flight_rollback_only_blocks_srb_clear_when_owned_child_cleanup_is_unconfirmed(tmp_path: Path):
    from agent.commands import ManagedMinerTransitionError

    for unconfirmed in (True, False):
        base = tmp_path / str(unconfirmed)
        base.mkdir()
        active = base / "active-flight-sheet.json"
        old_bytes = b'{"flight_sheet_id":"old-flight"}'
        active.write_bytes(old_bytes)
        cfg = config(
            state_file=base / "state.json",
            flight_sheet_file=active,
            managed_mining_enabled=True,
            managed_miner_runtime_dir=base / "miner-runtime",
        )
        document = {
            "flight_sheet_id": "target-flight",
            "worker_name": "fixture",
            "primary": {
                "miner_profile": "srb-fixture",
                "miner_type": "srbminer",
                "algorithm": "ethash",
                "miner_package": None,
            },
            "secondary": None,
            "gpu_oc": None,
        }
        events = []

        class Manager:
            phase = None

            def snapshot_independent_metadata(self):
                return {}

            def transition_has_live_processes(self):
                return True

            def begin_flight_transition(self, _document, *, previous_was_running):
                assert previous_was_running is True
                self.phase = "prepared"

            def mark_flight_transition_phase(self, _flight_id, phase):
                self.phase = phase

            def apply(self, _document):
                error = "owned child cleanup unconfirmed" if unconfirmed else "secondary metadata I/O failed"
                raise ManagedMinerTransitionError(
                    "target launch failed",
                    state_changed=True,
                    old_stopped=True,
                    target_started=True,
                    target_oc_may_have_applied=True,
                    target_cleanup_unconfirmed=unconfirmed,
                    rollback_errors=[error],
                )

            def clear_srb_gpu_oc_for_document(self, _document, *, restart_on_failure):
                assert restart_on_failure is False
                events.append("target-srb-clear")
                return {"cleared": True}

            def stop(self):
                events.append("stop")
                return {"stopped": not unconfirmed}

            def restore_independent_metadata(self, _snapshot):
                events.append("restore-metadata")

            def restart(self):
                events.append("old-restart")

            def finish_flight_transition(self, _flight_id):
                self.phase = "finished"

        manager = Manager()
        executor = CommandExecutor(cfg, managed_miner_factory=lambda _config: manager)
        with (
            mock.patch("agent.commands.validate_flight_sheet", return_value=document),
            mock.patch("agent.commands._active_flight_uses_srb_gpu_oc", return_value=False),
        ):
            outcome = executor.execute(Command("target-command", "apply_flight_sheet", {}))
        assert outcome.status == "failed"
        assert "old-restart" not in events
        assert manager.phase == "mutating"
        assert active.read_bytes() == old_bytes
        if unconfirmed:
            assert "target-srb-clear" not in events
            assert "owned child cleanup unconfirmed" in outcome.error
        else:
            assert events.count("target-srb-clear") == 1
            assert "secondary metadata I/O failed" in outcome.error


def test_srb_gpu_ownership_uses_selected_resource_and_legacy_defaults():
    from agent.commands import _flight_uses_srb_gpu

    def sheet(algorithm, extra=None, coin="TEST"):
        return {"primary": {"miner_type": "srbminer", "algorithm": algorithm,
                            "coin": coin, "extra_config": {} if extra is None else extra}, "secondary": None}

    assert _flight_uses_srb_gpu(sheet("yespower")) is False
    assert _flight_uses_srb_gpu(sheet("yespower", {"resource_kind": "cpu"})) is False
    assert _flight_uses_srb_gpu(sheet("yespower", {"resource_kind": "gpu"})) is True
    assert _flight_uses_srb_gpu(sheet("etchash")) is True
    assert _flight_uses_srb_gpu(sheet(None, coin="XMR")) is False
    assert _flight_uses_srb_gpu(sheet(None, coin="ETHW")) is True


def test_srb_gpu_ownership_excludes_wsl_non_srb_and_empty_roles():
    from agent.commands import _flight_uses_srb_gpu

    assert _flight_uses_srb_gpu({"primary": None, "secondary": None}) is False
    for miner_type in (None, "xmrig", "hiveos"):
        assert _flight_uses_srb_gpu({"primary": {"miner_type": miner_type, "algorithm": "etchash"}}) is False
    assert _flight_uses_srb_gpu({"primary": {"miner_type": "srbminer", "algorithm": "etchash",
                                          "execution_mode": "wsl"}}) is False


def test_srb_gpu_ownership_unknown_algorithm_or_resource_remains_unconfirmed():
    import pytest
    from agent.commands import _flight_uses_srb_gpu
    from agent.managed_miners import ManagedMinerError

    with pytest.raises(ManagedMinerError, match="unsupported"):
        _flight_uses_srb_gpu({"primary": {"miner_type": "srbminer", "algorithm": "unsupported-fixture"}})
    with pytest.raises(ManagedMinerError, match="resource"):
        _flight_uses_srb_gpu({"primary": {"miner_type": "srbminer", "algorithm": "randomx",
                                        "extra_config": {"resource_kind": "gpu"}}})
    with pytest.raises(ManagedMinerError, match="object"):
        _flight_uses_srb_gpu({"primary": {"miner_type": "srbminer", "algorithm": "etchash",
                                        "extra_config": None}})


def test_srb_gpu_ownership_empty_active_metadata_keeps_no_oc(tmp_path: Path):
    from agent.commands import _active_flight_uses_srb_gpu_oc

    assert _active_flight_uses_srb_gpu_oc(config(flight_sheet_file=None)) is False
    active = tmp_path / "flight.json"
    active.write_text('{"primary":null,"secondary":null}', encoding="utf-8")
    assert _active_flight_uses_srb_gpu_oc(config(flight_sheet_file=active, allow_gpu_oc=True)) is False


class UpdateForensicsTests(unittest.TestCase):
    """collect_update_forensics.v1: fixed read-only whitelist, Linux only."""

    @staticmethod
    def payload(**overrides):
        from agent.commands import UPDATE_FORENSICS_COMMANDS

        block = {
            "trigger": "agent_update_activation_timeout",
            "command_id": "01234567-89ab-4cde-8f01-23456789abcd",
            "source_version": "0.8.49",
            "target_version": "0.8.55",
            "commands": list(UPDATE_FORENSICS_COMMANDS),
        }
        block.update(overrides)
        return {"_controller_auto_forensics": block}

    def execute(self, payload, runner, system="Linux"):
        command = Command.parse(
            {
                "id": "cccccccc-dddd-4eee-8fff-000000000000",
                "action": "collect_update_forensics.v1",
                "payload": payload,
            }
        )
        executor = CommandExecutor(config(), runner=runner)
        with mock.patch("agent.commands.platform.system", return_value=system):
            return executor.execute(command)

    def test_runs_the_fixed_readonly_whitelist_and_reports_per_command_codes(self):
        from agent.commands import UPDATE_FORENSICS_COMMANDS

        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            index = [row[0][2] for row in calls].index(argv[2])
            codes = {0: 0, 1: 1, 2: 0}
            outputs = {
                0: "Filesystem Size Used Avail Use% Mounted on\n",
                1: "",
                2: "-- Logs begin at ...\n",
            }
            return mock.Mock(
                returncode=codes[index], stdout=outputs[index], stderr=""
            )

        outcome = self.execute(self.payload(), runner)
        self.assertEqual("succeeded", outcome.status)
        self.assertEqual(3, len(calls))
        for (argv, kwargs), literal in zip(calls, UPDATE_FORENSICS_COMMANDS):
            self.assertEqual(["/bin/bash", "-c", literal], argv)
            self.assertFalse(kwargs["shell"])
            self.assertIn("env", kwargs)
            self.assertEqual(30, kwargs["timeout"])
        result = outcome.result
        self.assertEqual("linux", result["platform"])
        self.assertEqual("agent_update_activation_timeout", result["trigger"])
        self.assertEqual(
            "01234567-89ab-4cde-8f01-23456789abcd", result["update_command_id"]
        )
        self.assertEqual("0.8.49", result["source_version"])
        self.assertEqual("0.8.55", result["target_version"])
        # The grep pipeline exiting 1 (no rigpilot timers) is a forensic
        # finding, not a collection failure.
        self.assertEqual(
            [0, 1, 0], [entry["return_code"] for entry in result["commands"]]
        )
        self.assertEqual(
            list(UPDATE_FORENSICS_COMMANDS),
            [entry["command"] for entry in result["commands"]],
        )

    def test_output_is_bounded_with_truncation_flag_and_result_fits_controller_limit(self):
        import json

        def runner(argv, **kwargs):
            return mock.Mock(
                returncode=0, stdout="x" * 90_000, stderr=""
            )

        outcome = self.execute(self.payload(), runner)
        self.assertEqual("succeeded", outcome.status)
        for entry in outcome.result["commands"]:
            self.assertEqual(8_192, len(entry["output"]))
            self.assertTrue(entry["output_truncated"])
            # Per-string controller limit is 8,192 characters: every output
            # must satisfy it individually, not only in aggregate.
            self.assertLessEqual(len(entry["output"]), 8_192)
        self.assertLess(
            len(json.dumps(outcome.result).encode("utf-8")), 131_072
        )

    def test_timeout_or_spawn_failure_is_recorded_per_command(self):
        def runner(argv, **kwargs):
            if "journalctl" in argv[2]:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
            return mock.Mock(returncode=0, stdout="ok\n", stderr="")

        outcome = self.execute(self.payload(), runner)
        self.assertEqual("succeeded", outcome.status)
        errors = [entry.get("error") for entry in outcome.result["commands"]]
        self.assertEqual(
            [None, None, "command timed out"], errors
        )
        self.assertEqual(
            [0, 0, None],
            [entry["return_code"] for entry in outcome.result["commands"]],
        )

    def test_rejects_any_payload_command_list_that_is_not_the_fixed_whitelist(self):
        from agent.commands import UPDATE_FORENSICS_COMMANDS

        runner = mock.Mock()
        tampered = list(UPDATE_FORENSICS_COMMANDS)
        tampered[1] = "cat /etc/shadow"
        for bad in (
            self.payload(commands=tampered),
            self.payload(commands=list(UPDATE_FORENSICS_COMMANDS) + ["id"]),
            self.payload(trigger="operator"),
            self.payload(command_id="not-a-uuid"),
            {"_controller_auto_forensics": {"trigger": "agent_update_activation_timeout"}},
            {"extra": True, "_controller_auto_forensics": self.payload()["_controller_auto_forensics"]},
        ):
            outcome = self.execute(bad, runner)
            self.assertEqual("failed", outcome.status, bad)
            self.assertEqual({"rejected": True}, outcome.result)
        runner.assert_not_called()

    def test_windows_is_rejected_quickly_without_running_anything(self):
        runner = mock.Mock()
        outcome = self.execute(self.payload(), runner, system="Windows")
        self.assertEqual("failed", outcome.status)
        self.assertEqual({"rejected": True}, outcome.result)
        self.assertIn("Linux-only", outcome.error or "")
        runner.assert_not_called()

    def test_dry_run_never_executes_forensics_commands(self):
        command = Command.parse(
            {
                "id": "cccccccc-dddd-4eee-8fff-000000000000",
                "action": "collect_update_forensics.v1",
                "payload": self.payload(),
            }
        )
        runner = mock.Mock()
        executor = CommandExecutor(config(dry_run=True), runner=runner)
        with mock.patch("agent.commands.platform.system", return_value="Linux"):
            outcome = executor.execute(command)
        self.assertEqual("succeeded", outcome.status)
        self.assertEqual(
            {"dry_run": True, "action": "collect_update_forensics.v1"},
            outcome.result,
        )
        runner.assert_not_called()
