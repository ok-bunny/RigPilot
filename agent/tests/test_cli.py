from __future__ import annotations

import pytest
import requests
from unittest import mock

from agent.client import ProtocolError
from agent.config import AgentConfig
from agent.runtime import AgentRuntime
from agent.state import AgentState, load_state, save_state

import agent.__main__ as cli
from agent.__main__ import build_parser


def test_version_flag_does_not_require_config(capsys: pytest.CaptureFixture[str]):
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"RigPilot Agent {cli.__version__}"


def test_third_party_notices_are_exact_and_exit_without_config_or_runtime(monkeypatch, capsys):
    from server.pci_identity import LICENSE_NOTICE

    def unexpected(*_args, **_kwargs):
        pytest.fail("license output must not load config or start the Agent")

    monkeypatch.setattr(cli, "load_config", unexpected)
    monkeypatch.setattr(cli, "AgentRuntime", unexpected)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--third-party-notices"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == LICENSE_NOTICE
    assert captured.err == ""


def test_frozen_memory_worker_dispatch_exits_before_config_or_runtime(monkeypatch):
    from agent import nvidia_memory

    def unexpected(*_args, **_kwargs):
        pytest.fail("fixed memory worker must not start normal Agent")

    calls = []
    monkeypatch.setattr(cli, "load_config", unexpected)
    monkeypatch.setattr(cli, "AgentRuntime", unexpected)
    monkeypatch.setattr(nvidia_memory, "worker_main", lambda: calls.append("worker") or 0)
    assert cli.main(["--nvidia-memory-worker"]) == 0
    assert calls == ["worker"]
    assert cli.main(["--nvidia-memory-worker", "--config", "unused.json"]) == 2
    assert calls == ["worker"]


def test_frozen_release_uses_packaged_config_defaults(monkeypatch):
    class Config:
        log_level = "INFO"
        log_file = None

        def with_dry_run(self, _enabled):
            return self

    captured: dict[str, object] = {}

    def load(path, *, packaged_defaults=False):
        captured["path"] = path
        captured["packaged_defaults"] = packaged_defaults
        return Config()

    class Runtime:
        def __init__(self, _config):
            pass

        def run(self, token, *, once=False, cli_replacement_token=None):
            captured["token"] = token
            captured["once"] = once
            captured["replacement_token"] = cli_replacement_token

    monkeypatch.setattr(cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(cli, "load_config", load)
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "AgentRuntime", Runtime)

    assert cli.main(["--config", "config.json", "--once"]) == 0
    assert captured == {
        "path": "config.json",
        "packaged_defaults": True,
        "token": None,
        "once": True,
        "replacement_token": None,
    }


def test_local_miner_transition_flags_are_mutually_exclusive():
    stop = build_parser().parse_args(
        ["--config", "config.json", "--stop-managed-mining-once"]
    )
    resume = build_parser().parse_args(
        ["--config", "config.json", "--resume-managed-mining-once"]
    )

    assert stop.stop_managed_mining_once is True
    assert stop.resume_managed_mining_once is False
    assert resume.stop_managed_mining_once is False
    assert resume.resume_managed_mining_once is True
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(
            [
                "--config",
                "config.json",
                "--stop-managed-mining-once",
                "--resume-managed-mining-once",
            ]
        )
    assert exc_info.value.code == 2


def test_private_miner_transition_flags_stay_out_of_public_help():
    help_text = build_parser().format_help()

    assert "--stop-managed-mining-once" not in help_text
    assert "--resume-managed-mining-once" not in help_text
    assert "--repair-remote-session-policy" not in help_text
    assert "--nvidia-driver-update-worker" not in help_text


@pytest.mark.parametrize(
    ("flag", "method", "expected"),
    [
        ("--stop-managed-mining-once", "stop", {"stopped": True}),
        ("--resume-managed-mining-once", "restart", {"restarted": True}),
    ],
)
def test_local_miner_transition_dispatches_without_starting_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    method: str,
    expected: dict[str, bool],
):
    class Config:
        log_level = "INFO"
        log_file = None

    calls: list[str] = []

    class Manager:
        def __init__(self, config):
            assert isinstance(config, Config)

        def stop(self):
            calls.append("stop")
            return {"stopped": True}

        def restart(self):
            calls.append("restart")
            return {"restarted": True}

    monkeypatch.setattr(cli, "load_config", lambda _path, **_kwargs: Config())
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "ManagedMinerManager", Manager)
    monkeypatch.setattr(
        cli,
        "AgentRuntime",
        lambda _config: pytest.fail("normal Agent runtime must not start"),
    )

    assert cli.main(["--config", "config.json", flag]) == 0
    assert calls == [method]
    assert __import__("json").loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    "failure",
    [
        requests.exceptions.SSLError("certificate verification failed"),
        requests.HTTPError("controller rejected the Agent credentials"),
        ProtocolError("heartbeat commands must be an array"),
    ],
)
def test_once_cli_fails_when_enrolled_identity_cannot_heartbeat(
    tmp_path, monkeypatch, capsys, failure
):
    config = AgentConfig(
        server_url="https://controller.test",
        state_file=tmp_path / "state.json",
    )
    state = AgentState.new("existing-agent", "private-agent-secret")
    save_state(config.state_file, state)
    runtime = AgentRuntime(config)
    runtime._host_data_sampler = mock.Mock()
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(runtime, "_host_data", lambda: {})
    heartbeat = mock.Mock(side_effect=failure)
    monkeypatch.setattr(runtime.client, "heartbeat", heartbeat)
    monkeypatch.setattr(cli, "load_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "AgentRuntime", lambda _config: runtime)

    assert cli.main(["--config", "config.json", "--once"]) == 2
    assert str(failure) in capsys.readouterr().err
    assert load_state(config.state_file) == state
    heartbeat.assert_called_once()
    runtime._host_data_sampler.close.assert_called_once_with()
