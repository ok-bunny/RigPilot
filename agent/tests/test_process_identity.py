from __future__ import annotations

from pathlib import Path

from agent.process_identity import (
    managed_argv_fingerprint,
    managed_process_argv_matches_record,
)


class _Process:
    def __init__(self, argv: list[str]) -> None:
        self._argv = argv

    def cmdline(self) -> list[str]:
        return list(self._argv)


def test_v3_record_requires_exact_wrapper_argv_fingerprint(tmp_path: Path):
    wrapper = tmp_path / "System32" / "wsl.exe"
    expected_argv = [str(wrapper), "--exec", "/mnt/c/packages/miner-cuda13"]
    record = {
        "version": 3,
        "miner_type": "pearlminer",
        "argv_sha256": managed_argv_fingerprint(
            expected_argv, executable=wrapper
        ),
    }

    assert managed_process_argv_matches_record(
        _Process(expected_argv), record=record, recorded_path=wrapper
    )
    assert not managed_process_argv_matches_record(
        _Process([str(wrapper)]), record=record, recorded_path=wrapper
    )


def test_native_linux_pearl_argv_scrub_exception_is_v2_only(tmp_path: Path):
    executable = tmp_path / "miner-cuda13"
    launch_argv = [str(executable), "--pool", "pool.example:1234"]
    fingerprint = managed_argv_fingerprint(launch_argv, executable=executable)
    scrubbed = _Process([str(executable), "", ""])

    assert managed_process_argv_matches_record(
        scrubbed,
        record={
            "version": 2,
            "miner_type": "pearlminer",
            "argv_sha256": fingerprint,
        },
        recorded_path=executable,
    )
    assert not managed_process_argv_matches_record(
        scrubbed,
        record={
            "version": 3,
            "miner_type": "pearlminer",
            "argv_sha256": fingerprint,
        },
        recorded_path=executable,
    )


def test_legacy_v1_record_keeps_path_and_creation_time_contract(tmp_path: Path):
    class _NoCmdline:
        def cmdline(self):
            raise AssertionError("v1 identity must not inspect argv")

    assert managed_process_argv_matches_record(
        _NoCmdline(),
        record={"version": 1},
        recorded_path=tmp_path / "miner",
    )


def test_unrepresentable_record_time_is_ambiguous_before_scan(tmp_path):
    from agent.process_identity import inspect_managed_process_replacement

    def forbidden_scan():
        raise AssertionError("invalid record must not inspect any process")

    result = inspect_managed_process_replacement(
        state_path=tmp_path / "record.json",
        record={"version": 2, "pid": 41, "create_time": 10 ** 1000,
                "argv_sha256": "a" * 64},
        recorded_path=tmp_path / "miner",
        process_iter_factory=forbidden_scan,
    )
    assert result.status == "ambiguous"
    assert result.reason == "invalid process record"
    assert result.rebound is None and result.explicit_candidate is None


def test_unrepresentable_live_time_cannot_be_adopted_or_selected(tmp_path):
    from agent.process_identity import inspect_managed_process_replacement

    executable = tmp_path / "miner"
    class Process:
        pid = 42
        def name(self): return "miner"
        def is_running(self): return True
        def status(self): return "running"
        def exe(self): return str(executable)
        def create_time(self): return 10 ** 1000
        def cmdline(self): return [str(executable)]

    result = inspect_managed_process_replacement(
        state_path=tmp_path / "record.json",
        record={"version": 2, "pid": 41, "create_time": 1.0,
                "argv_sha256": managed_argv_fingerprint([str(executable)])},
        recorded_path=executable, process_iter_factory=lambda: [Process()],
    )
    assert result.status == "ambiguous"
    assert result.rebound is None and result.explicit_candidate is None
    assert not (tmp_path / "record.json").exists()


def test_unrepresentable_cleanup_candidate_time_is_not_current(tmp_path):
    from agent.process_identity import (
        ManagedProcessCandidate, managed_process_candidate_is_current,
    )

    executable = tmp_path / "miner"
    class Process:
        pid = 42
        def is_running(self): return True
        def status(self): return "running"
        def exe(self): return str(executable)
        def create_time(self): return 10 ** 1000
        def cmdline(self): return [str(executable)]

    candidate = ManagedProcessCandidate(
        Process(), 42, 1.0, managed_argv_fingerprint([str(executable)]),
    )
    assert not managed_process_candidate_is_current(candidate, recorded_path=executable)


def test_finite_unrepresentable_utc_time_cannot_rebind_verified_process(tmp_path):
    from agent.process_identity import inspect_managed_process_replacement

    executable = tmp_path / "miner"
    class Process:
        pid = 42
        def name(self): return "miner"
        def is_running(self): return True
        def status(self): return "running"
        def exe(self): return str(executable)
        def create_time(self): return 1e100
        def cmdline(self): return [str(executable)]

    path = tmp_path / "record.json"
    path.write_bytes(b"original record unchanged")
    result = inspect_managed_process_replacement(
        state_path=path, record={"version": 2, "pid": 41, "create_time": 1.0,
                                "argv_sha256": managed_argv_fingerprint([str(executable)])},
        recorded_path=executable, process_iter_factory=lambda: [Process()],
    )
    assert result.status == "ambiguous"
    assert result.rebound is None and result.explicit_candidate is None
    assert path.read_bytes() == b"original record unchanged"


def test_unparseable_integer_process_record_cannot_authorize_cas(tmp_path):
    from agent.process_identity import _read_process_record, _replace_process_record

    path = tmp_path / "record.json"
    raw = ('{"invalid":' + '9' * 5000 + '}').encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    assert _read_process_record(path) is None
    assert _replace_process_record(path, {}, {"pid": 42}) is False
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob("*.tmp"))
