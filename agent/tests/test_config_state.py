from __future__ import annotations

import json
import logging
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

import agent.state as state_module
from agent.config import ConfigError, load_config
from agent.state import (
    AgentState,
    clear_pending_replacement,
    load_or_create_pending_replacement,
    load_state,
    replacement_pending_path,
    save_state,
)


class ConfigAndStateTests(unittest.TestCase):
    def test_posix_state_directory_entry_is_fsynced(self):
        directory = Path("/var/lib/rigpilot-agent")
        with (
            mock.patch.object(state_module.os, "name", "posix"),
            mock.patch.object(state_module.os, "open", return_value=91) as opened,
            mock.patch.object(state_module.os, "fsync") as fsynced,
            mock.patch.object(state_module.os, "close") as closed,
        ):
            state_module._fsync_directory(directory)

        opened.assert_called_once_with(
            str(directory),
            state_module.os.O_RDONLY | getattr(state_module.os, "O_DIRECTORY", 0),
        )
        fsynced.assert_called_once_with(91)
        closed.assert_called_once_with(91)

    def test_windows_utf8_bom_config_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            path.write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                    }
                ),
                encoding="utf-8-sig",
            )

            config = load_config(path)

            self.assertEqual("https://example.test", config.server_url)

    def test_relative_ca_bundle_is_resolved_from_config_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "private-ca.pem").write_text("test-ca", encoding="utf-8")
            (root / "agent.json").write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                        "ca_bundle": "private-ca.pem",
                        "log_file": "logs/agent.jsonl",
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(root / "agent.json")

            self.assertEqual(str((root / "private-ca.pem").resolve()), config.ca_bundle)
            self.assertEqual((root / "logs" / "agent.jsonl").resolve(), config.log_file)

    def test_history_trimming_uses_completion_time_not_sorted_command_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = AgentState.new("agent-id", "agent-secret")
            state.command_history = {
                "z-old": {"completed_at": "2026-01-01T00:00:00+00:00"},
                "a-new": {"completed_at": "2026-02-01T00:00:00+00:00"},
            }

            save_state(path, state, history_limit=1)
            loaded = load_state(path)

            self.assertIsNotNone(loaded)
            self.assertEqual(["a-new"], list(loaded.command_history))

    def test_replacement_pending_material_is_stable_and_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            first = load_or_create_pending_replacement(
                state_path, "https://controller.test", "rpr_" + "A" * 43
            )
            second = load_or_create_pending_replacement(
                state_path, "https://controller.test/", "rpr_" + "A" * 43
            )

            self.assertEqual(first, second)
            self.assertRegex(first.request_id, r"^[0-9a-f]{32}$")
            self.assertRegex(
                first.proposed_agent_secret,
                r"^rpa_[A-Za-z0-9_-]{48}$",
            )
            pending_path = replacement_pending_path(state_path)
            self.assertTrue(pending_path.is_file())
            clear_pending_replacement(state_path)
            self.assertFalse(pending_path.exists())

    def test_malformed_server_url_and_controller_only_download_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            malformed = root / "malformed.json"
            malformed.write_text(
                json.dumps(
                    {
                        "server_url": "https://[::1/api",
                        "state_file": "state.json",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "malformed"):
                load_config(malformed)

            unpinned = root / "unpinned.json"
            unpinned.write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                        "flight_sheet_file": "flight.json",
                        "miner_profile_allowlist": ["safe-miner"],
                        "allow_miner_downloads": True,
                        "miner_packages_dir": "packages",
                        "miner_download_host_allowlist": [
                            "downloads.example.test"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            controller_only = load_config(unpinned)
            self.assertFalse(
                hasattr(controller_only, "miner_package_sha256_allowlist")
            )

            pinned_data = json.loads(unpinned.read_text(encoding="utf-8"))
            pinned_data["miner_package_sha256_allowlist"] = {
                "removed-profile": "legacy-invalid-value"
            }
            pinned = root / "pinned.json"
            pinned.write_text(json.dumps(pinned_data), encoding="utf-8")
            loaded = load_config(pinned)
            self.assertFalse(hasattr(loaded, "miner_package_sha256_allowlist"))

    def test_relative_miner_status_file_is_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.json"
            path.write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                        "miner_status_file": "miner/status.json",
                    }
                ),
                encoding="utf-8",
            )

            loaded = load_config(path)

            self.assertEqual((root / "miner/status.json").resolve(), loaded.miner_status_file)

    def test_managed_mining_defaults_off_and_requires_a_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            default_path = root / "default.json"
            default_path.write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                    }
                ),
                encoding="utf-8",
            )
            default = load_config(default_path)
            self.assertFalse(default.managed_mining_enabled)
            self.assertIsNone(default.managed_miner_runtime_dir)
            self.assertFalse(default.allow_agent_updates)
            self.assertEqual(512, default.agent_update_max_mb)
            self.assertEqual(900.0, default.agent_update_total_timeout)

            missing = root / "missing.json"
            missing.write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                        "managed_mining_enabled": True,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "runtime_dir is required"):
                load_config(missing)

            enabled = root / "enabled.json"
            enabled.write_text(
                json.dumps(
                    {
                        "server_url": "https://example.test",
                        "state_file": "state.json",
                        "managed_mining_enabled": True,
                        "managed_miner_runtime_dir": "managed-runtime",
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_config(enabled)
            runtime = (root / "managed-runtime").resolve()
            self.assertEqual(runtime, loaded.managed_miner_runtime_dir)
            self.assertEqual(runtime / "miner-status.json", loaded.miner_status_file)


if __name__ == "__main__":
    unittest.main()


def test_legacy_miner_self_heal_settings_are_inert_after_upgrade(tmp_path: Path):
    path = tmp_path / "config.json"
    base = {"server_url": "https://controller.example", "state_file": "state.json"}
    path.write_text(json.dumps(base), encoding="utf-8")
    default = load_config(path)
    legacy = {
        "miner_self_heal_enabled": True,
        "miner_self_heal_l1_enabled": True,
        "miner_self_heal_l2_enabled": True,
        "miner_self_heal_l3_enabled": True,
        "miner_self_heal_max_attempts": 4,
        "miner_self_heal_backoff_base_seconds": 30,
        "miner_self_heal_backoff_max_seconds": 600,
        "miner_self_heal_gpu_settle_seconds": 1,
        "miner_self_heal_driver_reload_limit": 7,
    }
    path.write_text(json.dumps({**base, **legacy}), encoding="utf-8")
    upgraded = load_config(path)
    assert upgraded == default
    assert not any(hasattr(upgraded, name) for name in legacy)
    path.write_text(json.dumps({**base, "miner_self_heal_unknown": True}), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown configuration keys"):
        load_config(path)


@pytest.mark.parametrize("old_value", [True, False, None, "legacy", {}, []])
def test_retired_wifi_setting_is_ignored_after_binary_only_update(
    tmp_path: Path, old_value: bool
):
    path = tmp_path / "agent.json"
    path.write_text(
        json.dumps(
            {
                "server_url": "https://controller.example",
                "state_file": "state.json",
                "allow_agent_updates": True,
                "allow_wifi_management": old_value,
            }
        ),
        encoding="utf-8",
    )
    for packaged_defaults in (False, True):
        config = load_config(path, packaged_defaults=packaged_defaults)
        assert config.allow_agent_updates is True
        assert not hasattr(config, "allow_wifi_management")


def test_json_integer_parser_rejection_is_a_config_error(tmp_path):
    import sys

    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit:
        pytest.skip("Python integer conversion has no configured digit limit")
    path = tmp_path / "config.json"
    content = '{"server_url":"https://controller.test","request_timeout":' + "1" * (limit + 1) + "}"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON configuration"):
        load_config(path)
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["heartbeat_interval", "request_timeout"])
def test_nonfinite_polling_config_is_rejected(tmp_path: Path, field, value):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({
        "server_url": "https://controller.test",
        "state_file": "state.json",
        field: value,
    }), encoding="utf-8")

    with pytest.raises(ConfigError, match=field):
        load_config(path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_persisted_heartbeat_interval_is_rejected(tmp_path: Path, value):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "agent_id": "existing-agent",
        "agent_secret": "private-agent-secret",
        "enrolled_at": "2026-09-08T00:00:00+00:00",
        "heartbeat_interval": value,
    }), encoding="utf-8")

    with pytest.raises(state_module.StateError, match="heartbeat_interval"):
        load_state(path)


_VALID_STATE = {
    "agent_id": "existing-agent",
    "agent_secret": "private-agent-secret",
    "enrolled_at": "2026-09-08T00:00:00+00:00",
}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_widened_state_file_mode_is_repaired_and_loaded(tmp_path: Path, caplog):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_VALID_STATE), encoding="utf-8")
    path.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="agent.state"):
        loaded = load_state(path)

    assert loaded is not None and loaded.agent_id == "existing-agent"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    events = [
        record for record in caplog.records
        if getattr(record, "event", None) == "state_file_mode_repaired"
    ]
    assert events


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_foreign_owned_state_file_is_rejected(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_VALID_STATE), encoding="utf-8")
    path.chmod(0o600)
    foreign = state_module.os.geteuid() + 1
    monkeypatch.setattr(state_module.os, "geteuid", lambda: foreign)

    with pytest.raises(state_module.StateError, match="owned by another user"):
        load_state(path)


def test_symlinked_state_file_is_rejected(tmp_path: Path):
    real = tmp_path / "real-state.json"
    real.write_text(json.dumps(_VALID_STATE), encoding="utf-8")
    link = tmp_path / "state.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable here: {exc}")

    with pytest.raises(state_module.StateError, match="private regular local file"):
        load_state(link)


def test_oversized_state_file_is_rejected_without_reading(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_VALID_STATE), encoding="utf-8")
    with open(path, "ab") as handle:
        handle.truncate(state_module._MAX_STATE_FILE_BYTES + 1)

    with pytest.raises(state_module.StateError, match="private regular local file"):
        load_state(path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
def test_unrepairable_state_file_mode_is_a_hard_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_VALID_STATE), encoding="utf-8")
    path.chmod(0o644)

    def refused(_path, _mode):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(state_module.os, "chmod", refused)
    with pytest.raises(state_module.StateError, match="cannot be repaired"):
        load_state(path)


@pytest.mark.parametrize("bounds,expected", [
    ({}, (None, None)),
    ({"gpu_power_limit_min_w": None, "gpu_power_limit_max_w": None}, (None, None)),
    ({"gpu_power_limit_min_w": 15}, (15.0, None)),
    ({"gpu_power_limit_max_w": 360}, (None, 360.0)),
    ({"gpu_power_limit_min_w": 50, "gpu_power_limit_max_w": 300}, (50.0, 300.0)),
    ({"gpu_power_limit_min_w": 5, "gpu_power_limit_max_w": 1200}, (5.0, 1200.0)),
])
def test_power_policy_bounds_are_optional_or_explicit_positive_finite(tmp_path, bounds, expected):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"server_url": "https://controller.example", "state_file": "state.json", **bounds}))
    cfg = load_config(path)
    assert (cfg.gpu_power_limit_min_w, cfg.gpu_power_limit_max_w) == expected


@pytest.mark.parametrize("field", ["gpu_power_limit_min_w", "gpu_power_limit_max_w"])
@pytest.mark.parametrize("value", [True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "300"])
def test_explicit_power_policy_bound_rejects_invalid_values(tmp_path, field, value):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"server_url": "https://controller.example", "state_file": "state.json", field: value}))
    with pytest.raises(ConfigError, match=field):
        load_config(path)


def test_power_policy_rejects_reversed_explicit_bounds(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"server_url": "https://controller.example", "state_file": "state.json", "gpu_power_limit_min_w": 360, "gpu_power_limit_max_w": 300}))
    with pytest.raises(ConfigError, match="minimum must not exceed maximum"):
        load_config(path)


@pytest.mark.parametrize("limit_mb", [1, 16, 32, 512, 2048])
def test_update_download_budget_accepts_positive_small_limits(tmp_path, limit_mb):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({
        "server_url": "https://controller.example",
        "agent_update_max_mb": limit_mb,
    }), encoding="utf-8")
    assert load_config(path).agent_update_max_mb == limit_mb


@pytest.mark.parametrize("limit_mb", [0, -1, True, 1.5, "16", 2049])
def test_update_download_budget_keeps_type_positive_and_upper_limits(tmp_path, limit_mb):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({
        "server_url": "https://controller.example",
        "agent_update_max_mb": limit_mb,
    }), encoding="utf-8")
    with pytest.raises(ConfigError, match="agent_update_max_mb"):
        load_config(path)
