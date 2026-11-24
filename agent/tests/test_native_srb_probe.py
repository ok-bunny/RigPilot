from __future__ import annotations

from types import SimpleNamespace
import subprocess

import pytest

from agent.native_srb_probe import run_native_srb_probe
import agent.native_srb_probe as probe


ARGV = [r"C:\RigPilot\bundle\SRBMiner-MULTI.exe", "--list-devices"]


@pytest.mark.parametrize("returncode", [0, 2])
def test_completed_run_exposes_only_direct_child_exit(monkeypatch, returncode):
    result = subprocess.CompletedProcess(ARGV, returncode, "devices", "diagnostic")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return result

    monkeypatch.setattr(subprocess, "run", runner)
    environment = {"PATH": "owned-test-path"}
    actual = run_native_srb_probe(ARGV, system_name="Windows", cwd=r"C:\RigPilot\bundle",
                                  timeout=15, env=environment)
    assert actual is result and actual.owned_direct_child_closed is True
    assert not hasattr(actual, "owned_scope_closed")
    assert actual.returncode == returncode and actual.stdout == "devices" and actual.stderr == "diagnostic"
    assert calls == [(ARGV, dict(cwd=r"C:\RigPilot\bundle", shell=False, check=False,
                                capture_output=True, text=True, timeout=15, env=environment))]


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired(ARGV, 15, output="partial", stderr="failure"),
    PermissionError("process creation denied"),
    OSError("child wait unavailable"),
])
def test_exception_keeps_its_type_output_and_uncertainty_without_rerun(monkeypatch, error):
    calls = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(type(error)) as caught:
        run_native_srb_probe(ARGV, system_name="Windows", cwd="owned", timeout=15)
    assert caught.value is error and error.owned_direct_child_closed is False
    assert len(calls) == 1
    if isinstance(error, subprocess.TimeoutExpired):
        assert error.stdout == "partial" and error.stderr == "failure"


@pytest.mark.parametrize("change", [
    {"system_name": "Linux"}, {"shell": True}, {"check": True},
    {"text": False}, {"capture_output": False},
    {"argv": [ARGV[0], "--mine"]}, {"argv": [*ARGV, "extra"]},
])
def test_adapter_does_not_expand_the_existing_fixed_probe_cli(monkeypatch, change):
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("invalid CLI must not run"))
    arguments = dict(argv=ARGV, system_name="Windows", cwd="owned", timeout=15)
    arguments.update(change)
    with pytest.raises(ValueError) as caught:
        run_native_srb_probe(**arguments)
    assert caught.value.owned_direct_child_closed is False


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=0, stdout="", stderr=""),
    subprocess.CompletedProcess(ARGV, True, "", ""),
])
def test_unrecognized_run_result_never_manufactures_exit_proof(monkeypatch, result):
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: result)
    with pytest.raises(RuntimeError) as caught:
        run_native_srb_probe(ARGV, system_name="Windows", cwd="owned", timeout=15)
    assert caught.value.owned_direct_child_closed is False


def test_default_windows_run_timeout_keeps_original_error_and_kill_wait_proof(monkeypatch):
    error = subprocess.TimeoutExpired(ARGV, 15, output="old-partial", stderr="old-error")
    events = []

    class Child:
        def __init__(self, *_args, **_kwargs):
            self.args = ARGV
            self.returncode = None

        def __enter__(self):
            return self

        def __exit__(self, *_exception):
            self.wait()

        def communicate(self, _input=None, timeout=None):
            events.append(("communicate", timeout))
            if timeout is not None:
                raise error
            self.wait()
            return "final-partial", "final-error"

        def kill(self):
            events.append(("kill",))

        def wait(self):
            events.append(("wait",))
            self.returncode = -9
            return -9

    # Actual default subprocess.run control flow; only its OS child boundary
    # is mocked locally. No real Windows execution is claimed by this test.
    monkeypatch.setattr(subprocess, "run", probe._DEFAULT_RUN)
    monkeypatch.setattr(subprocess, "Popen", Child)
    monkeypatch.setattr(subprocess, "_mswindows", True)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_native_srb_probe(ARGV, system_name="Windows", cwd="owned", timeout=15)
    assert caught.value is error and error.owned_direct_child_closed is True
    assert error.stdout == "final-partial" and error.stderr == "final-error"
    assert events == [("communicate", 15), ("kill",), ("communicate", None), ("wait",), ("wait",)]
