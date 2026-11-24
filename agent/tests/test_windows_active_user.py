from __future__ import annotations

import ctypes
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import agent.windows_active_user as module
from agent.windows_active_user import (
    ActiveUserProcess,
    WindowsActiveUserError,
    popen,
    run,
)


def test_command_line_uses_subprocess_windows_quoting(monkeypatch):
    observed = {}

    def list2cmdline(argv):
        observed["argv"] = argv
        return "quoted command line"

    monkeypatch.setattr(module.subprocess, "list2cmdline", list2cmdline)

    assert module._command_line(["tool.exe", "a b"]) == "quoted command line"
    assert observed["argv"] == ["tool.exe", "a b"]


@pytest.mark.parametrize(
    "argv, error",
    [
        ("tool.exe --option", TypeError),
        ([], ValueError),
        ([""], ValueError),
        (["tool.exe", "bad\x00value"], ValueError),
    ],
)
def test_argv_rejects_command_text_and_invalid_values(argv, error):
    with pytest.raises(error):
        module._argv_strings(argv)


def test_non_windows_platform_is_rejected(monkeypatch):
    monkeypatch.setattr(module, "_is_windows", lambda: False)

    with pytest.raises(WindowsActiveUserError, match="Windows-only"):
        run(["tool"])


def test_non_system_run_falls_back_to_subprocess_without_shell(monkeypatch):
    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_running_as_local_system", lambda: False)
    completed = subprocess.CompletedProcess(["tool.exe"], 7, b"out", b"err")
    runner = mock.Mock(return_value=completed)
    monkeypatch.setattr(
        module,
        "subprocess",
        SimpleNamespace(
            run=runner,
            DEVNULL=subprocess.DEVNULL,
            CREATE_NO_WINDOW=0x08000000,
        ),
    )

    result = run([Path("tool.exe"), "a b"], timeout=3, cwd=Path("C:/work"))

    assert result is completed
    assert (result.stdout, result.stderr, result.returncode) == ("out", "err", 7)
    runner.assert_called_once_with(
        ["tool.exe", "a b"],
        cwd=str(Path("C:/work")),
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=False,
        timeout=3,
        check=False,
        creationflags=0x08000000,
    )


@pytest.mark.parametrize("returncode", [0, 7])
def test_non_system_run_preserves_utf8_wsl_paths_and_native_stderr(monkeypatch, returncode):
    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_running_as_local_system", lambda: False)
    monkeypatch.setattr(module.locale, "getpreferredencoding", lambda _setlocale: "cp1252")
    path = "/mnt/c/矿工目录/miner"
    completed = subprocess.CompletedProcess(["wsl.exe"], returncode, path.encode("utf-8"), b"native \x80")
    runner = mock.Mock(return_value=completed)
    monkeypatch.setattr(module.subprocess, "run", runner)

    assert run(["wsl.exe", "--exec", "wslpath", "C:\\矿工目录\\miner"]) is completed
    assert completed.stdout == path
    assert completed.stderr == "native €"
    assert completed.returncode == returncode
    assert runner.call_count == 1
    assert runner.call_args.kwargs["text"] is False


@pytest.mark.parametrize("captured", [False, True])
def test_non_system_timeout_keeps_original_exception_and_decodes_partial_output(monkeypatch, captured):
    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_running_as_local_system", lambda: False)
    monkeypatch.setattr(module.locale, "getpreferredencoding", lambda _setlocale: "cp1252")
    error = subprocess.TimeoutExpired(["wsl.exe"], 3,
        output="已输出路径".encode("utf-8") if captured else None,
        stderr=b"native \x80" if captured else None)
    runner = mock.Mock(side_effect=error)
    monkeypatch.setattr(module.subprocess, "run", runner)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run(["wsl.exe"], timeout=3)
    assert raised.value is error
    assert error.cmd == ["wsl.exe"] and error.timeout == 3
    assert error.stdout == ("已输出路径" if captured else None)
    assert error.stderr == ("native €" if captured else None)
    assert runner.call_count == 1


def test_non_system_launch_error_is_not_replaced_or_retried(monkeypatch):
    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_running_as_local_system", lambda: False)
    error = OSError("Cannot start owned WSL command")
    runner = mock.Mock(side_effect=error)
    monkeypatch.setattr(module.subprocess, "run", runner)
    with pytest.raises(OSError) as raised:
        run(["wsl.exe"])
    assert raised.value is error and runner.call_count == 1


def test_non_system_popen_combines_output_and_uses_process_group(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_running_as_local_system", lambda: False)
    process = SimpleNamespace(pid=123)
    factory = mock.Mock(return_value=process)
    monkeypatch.setattr(
        module,
        "subprocess",
        SimpleNamespace(
            Popen=factory,
            DEVNULL=subprocess.DEVNULL,
            STDOUT=subprocess.STDOUT,
            CREATE_NO_WINDOW=0x08000000,
            CREATE_NEW_PROCESS_GROUP=0x00000200,
        ),
    )

    with (tmp_path / "miner.log").open("ab", buffering=0) as output:
        result = popen(["miner.exe", "--flag"], stdout=output, cwd=tmp_path)

        assert result is process
        factory.assert_called_once_with(
            ["miner.exe", "--flag"],
            cwd=str(tmp_path),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=0x08000200,
        )


def test_system_paths_dispatch_to_active_user_implementations(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "_is_windows", lambda: True)
    monkeypatch.setattr(module, "_running_as_local_system", lambda: True)
    completed = subprocess.CompletedProcess(["wsl.exe"], 0, "ok", "")
    run_impl = mock.Mock(return_value=completed)
    popen_impl = mock.Mock(return_value=SimpleNamespace(pid=456, _handle=789))
    monkeypatch.setattr(module, "_active_user_run", run_impl)
    monkeypatch.setattr(module, "_active_user_popen", popen_impl)

    assert run(["wsl.exe", "-l"], timeout=5, cwd=tmp_path) is completed
    run_impl.assert_called_once_with(
        ["wsl.exe", "-l"], timeout=5, cwd=str(tmp_path)
    )

    with (tmp_path / "miner.log").open("ab", buffering=0) as output:
        process = popen(["wsl.exe", "--exec", "miner"], stdout=output, cwd=tmp_path)
        assert process.pid == 456
        popen_impl.assert_called_once_with(
            ["wsl.exe", "--exec", "miner"], stdout=output, cwd=str(tmp_path)
        )


def test_missing_active_console_session_has_clear_error(monkeypatch):
    fake_api = SimpleNamespace(
        kernel32=SimpleNamespace(
            WTSGetActiveConsoleSessionId=lambda: module._INVALID_SESSION_ID
        )
    )
    monkeypatch.setattr(module, "_get_api", lambda: fake_api)

    with pytest.raises(WindowsActiveUserError, match="no active Windows console user"):
        module._spawn_as_active_user(
            ["wsl.exe"],
            stdin_handle=1,
            stdout_handle=2,
            stderr_handle=2,
            cwd=None,
        )


def test_handle_list_contains_only_unique_standard_io_handles():
    observed = {"deleted": 0}

    class Kernel32:
        @staticmethod
        def InitializeProcThreadAttributeList(pointer, count, flags, required):
            assert count == 1
            assert flags == 0
            ctypes.cast(required, ctypes.POINTER(ctypes.c_size_t)).contents.value = 96
            return bool(pointer)

        @staticmethod
        def UpdateProcThreadAttribute(
            attribute_list,
            flags,
            attribute,
            value,
            size,
            previous,
            returned_size,
        ):
            assert attribute_list
            assert flags == 0
            assert attribute == module._PROC_THREAD_ATTRIBUTE_HANDLE_LIST
            assert previous is None
            assert returned_size is None
            count = size // ctypes.sizeof(module.wintypes.HANDLE)
            values = ctypes.cast(
                value,
                ctypes.POINTER(module.wintypes.HANDLE * count),
            ).contents
            observed["handles"] = [int(item or 0) for item in values]
            return True

        @staticmethod
        def DeleteProcThreadAttributeList(_attribute_list):
            observed["deleted"] += 1

    api = SimpleNamespace(
        kernel32=Kernel32(),
        error=lambda action: WindowsActiveUserError(action),
    )

    storage, attribute_list, handles = module._initialize_handle_list(
        api, (11, 22, 22)
    )

    assert storage is not None
    assert attribute_list
    assert len(handles) == 2
    assert observed["handles"] == [11, 22]
    # Ownership remains with the caller until CreateProcessAsUserW returns.
    api.kernel32.DeleteProcThreadAttributeList(attribute_list)
    assert observed["deleted"] == 1


def test_active_user_spawn_uses_extended_startup_and_explicit_handle_list(
    monkeypatch,
):
    observed = {"closed": [], "deleted": 0, "destroyed": 0}

    class Kernel32:
        @staticmethod
        def WTSGetActiveConsoleSessionId():
            return 7

        @staticmethod
        def DeleteProcThreadAttributeList(attribute_list):
            assert int(attribute_list.value or 0) == 0x1234
            observed["deleted"] += 1

    class WtsApi32:
        @staticmethod
        def WTSQueryUserToken(session_id, token):
            assert session_id == 7
            ctypes.cast(
                token, ctypes.POINTER(module.wintypes.HANDLE)
            ).contents.value = 101
            return True

    class UserEnv:
        @staticmethod
        def CreateEnvironmentBlock(environment, token, inherit):
            assert int(token.value or 0) == 202
            assert inherit is False
            ctypes.cast(
                environment, ctypes.POINTER(module.wintypes.LPVOID)
            ).contents.value = 303
            return True

        @staticmethod
        def DestroyEnvironmentBlock(environment):
            assert int(environment.value or 0) == 303
            observed["destroyed"] += 1
            return True

    class AdvApi32:
        @staticmethod
        def DuplicateTokenEx(
            user_token,
            access,
            security,
            impersonation,
            token_type,
            primary_token,
        ):
            assert int(user_token.value or 0) == 101
            assert access == module._MAXIMUM_ALLOWED
            assert security is None
            assert impersonation == module._SECURITY_IMPERSONATION
            assert token_type == module._TOKEN_PRIMARY
            ctypes.cast(
                primary_token, ctypes.POINTER(module.wintypes.HANDLE)
            ).contents.value = 202
            return True

        @staticmethod
        def CreateProcessAsUserW(*args):
            (
                primary_token,
                application,
                _command_line,
                process_security,
                thread_security,
                inherit_handles,
                creation_flags,
                environment,
                cwd,
                startup_pointer,
                process_info_pointer,
            ) = args
            assert int(primary_token.value or 0) == 202
            assert application is None
            assert process_security is None
            assert thread_security is None
            assert inherit_handles is True
            assert creation_flags & module._EXTENDED_STARTUPINFO_PRESENT
            assert int(environment.value or 0) == 303
            assert cwd == "C:\\work"
            startup = ctypes.cast(
                startup_pointer,
                ctypes.POINTER(module._STARTUPINFOEXW),
            ).contents
            assert startup.StartupInfo.cb == ctypes.sizeof(module._STARTUPINFOEXW)
            assert int(startup.lpAttributeList or 0) == 0x1234
            assert int(startup.StartupInfo.hStdInput or 0) == 11
            assert int(startup.StartupInfo.hStdOutput or 0) == 22
            assert int(startup.StartupInfo.hStdError or 0) == 22
            process_info = ctypes.cast(
                process_info_pointer,
                ctypes.POINTER(module._PROCESS_INFORMATION),
            ).contents
            process_info.hProcess = 404
            process_info.hThread = 505
            process_info.dwProcessId = 606
            process_info.dwThreadId = 707
            return True

    api = SimpleNamespace(
        kernel32=Kernel32(),
        wtsapi32=WtsApi32(),
        userenv=UserEnv(),
        advapi32=AdvApi32(),
        close_handle=lambda handle: (
            observed["closed"].append(handle) if handle else None
        ),
        error=lambda action: WindowsActiveUserError(action),
    )
    handle_array = (module.wintypes.HANDLE * 2)(11, 22)
    monkeypatch.setattr(module, "_get_api", lambda: api)
    monkeypatch.setattr(
        module,
        "_initialize_handle_list",
        lambda _api, handles: (
            ctypes.create_string_buffer(16),
            module.wintypes.LPVOID(0x1234),
            handle_array,
        ),
    )

    process = module._spawn_as_active_user(
        ["wsl.exe", "--exec", "miner"],
        stdin_handle=11,
        stdout_handle=22,
        stderr_handle=22,
        cwd="C:\\work",
    )

    assert process.pid == 606
    assert process._handle == 404
    assert observed["deleted"] == 1
    assert observed["destroyed"] == 1
    assert observed["closed"] == [505, 202, 101]
    process.close()
    assert observed["closed"] == [505, 202, 101, 404]


class _FakeProcessApi:
    def __init__(self):
        self.wait_results = [module._WAIT_TIMEOUT, module._WAIT_OBJECT_0]
        self.closed = []
        self.terminated = []

    def wait(self, _handle, _milliseconds):
        return self.wait_results.pop(0)

    def exit_code(self, _handle):
        return 23

    def terminate(self, handle, exit_code):
        self.terminated.append((handle, exit_code))

    def close_handle(self, handle):
        self.closed.append(handle)

    @staticmethod
    def error(action):
        return WindowsActiveUserError(action)


def test_active_user_process_exposes_popen_compatible_lifecycle(monkeypatch):
    api = _FakeProcessApi()
    api.wait_results = [
        module._WAIT_TIMEOUT,
        module._WAIT_TIMEOUT,
        module._WAIT_OBJECT_0,
    ]
    monkeypatch.setattr(module, "_get_api", lambda: api)
    process = ActiveUserProcess(["miner.exe"], 42, 99)

    assert process.pid == 42
    assert process._handle == 99
    assert process.poll() is None
    process.terminate()
    assert api.terminated == [(99, 1)]
    assert process.wait(timeout=1) == 23
    process.kill()  # Already exited; must not terminate a reused PID/handle.
    process.close()
    process.close()
    assert api.closed == [99]


def test_active_user_process_wait_raises_standard_timeout(monkeypatch):
    api = _FakeProcessApi()
    api.wait_results = [module._WAIT_TIMEOUT]
    monkeypatch.setattr(module, "_get_api", lambda: api)
    process = ActiveUserProcess(["miner.exe"], 42, 99)

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        process.wait(timeout=0.25)

    assert exc_info.value.cmd == ["miner.exe"]
    process.close()


def test_closed_process_error_precedes_invalid_timeout_and_cached_exit_still_wins(monkeypatch):
    api = _FakeProcessApi()
    api.wait_results = []
    monkeypatch.setattr(module, "_get_api", lambda: api)
    process = ActiveUserProcess(["miner.exe"], 42, 99)
    process.close()
    with pytest.raises(WindowsActiveUserError, match="already closed"):
        process.wait(timeout=-1)
    process.returncode = 23
    assert process.wait(timeout=-1) == 23
    assert process.poll() == 23
    assert api.closed == [99]


@pytest.mark.parametrize("method", ["poll", "wait"])
@pytest.mark.parametrize("native_status", [module._WAIT_FAILED, 0x80])
def test_active_user_process_preserves_native_wait_failures(monkeypatch, method, native_status):
    api = _FakeProcessApi()
    api.wait_results = [native_status]
    monkeypatch.setattr(module, "_get_api", lambda: api)
    process = ActiveUserProcess(["miner.exe"], 42, 99)
    try:
        with pytest.raises(WindowsActiveUserError, match="WaitForSingleObject"):
            getattr(process, method)()
        assert process.returncode is None
    finally:
        process.close()


def test_timeout_conversion_is_bounded():
    assert module._timeout_milliseconds(None) == module._INFINITE
    assert module._timeout_milliseconds(0) == 0
    assert module._timeout_milliseconds(0.0011) == 2
    with pytest.raises(ValueError):
        module._timeout_milliseconds(-1)


def test_active_user_output_decodes_wsl_utf8_before_windows_code_page(
    monkeypatch,
):
    monkeypatch.setattr(
        module.locale,
        "getpreferredencoding",
        lambda _setlocale: "cp1252",
    )

    assert module._decode_output("WSL 检测成功".encode("utf-8")) == "WSL 检测成功"


def test_active_user_output_falls_back_for_native_windows_encoding(monkeypatch):
    monkeypatch.setattr(
        module.locale,
        "getpreferredencoding",
        lambda _setlocale: "cp1252",
    )

    assert module._decode_output(b"native \x80 output") == "native € output"


@pytest.mark.parametrize("failure", ["wait", "timeout"])
def test_active_user_run_closes_process_handle_on_wait_failure_or_timeout(monkeypatch, failure):
    closed = []
    handle_closes = []
    waits = []
    kills = []

    class Process:
        def wait(self, timeout):
            waits.append(timeout)
            if failure == "wait":
                raise WindowsActiveUserError("mock WaitForSingleObject failed")
            if len(waits) == 1:
                raise subprocess.TimeoutExpired(["tool.exe"], timeout)
            return 1

        def kill(self):
            kills.append(True)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "_get_api", lambda: SimpleNamespace(close_handle=handle_closes.append))
    monkeypatch.setattr(module, "_open_inheritable_null_input", lambda: 10)
    monkeypatch.setattr(module, "_native_file_handle", lambda _file: 20)
    monkeypatch.setattr(module, "_duplicate_inheritable_handle", lambda _source: 30)
    monkeypatch.setattr(module, "_spawn_as_active_user", lambda *_args, **_kwargs: Process())
    expected = WindowsActiveUserError if failure == "wait" else subprocess.TimeoutExpired
    with pytest.raises(expected) as error:
        module._active_user_run(["tool.exe"], timeout=0.1, cwd=None)
    assert closed == [True]
    assert handle_closes == [30, 30, 10]
    assert kills == ([] if failure == "wait" else [True])
    if failure == "timeout":
        assert error.value.output == "" and error.value.stderr == ""


@pytest.mark.parametrize("timeout, expected", [(None, 0xffffffff), (0, 0), (0.0001, 1), (1.2, 1200), (1e308, 0xfffffffe)])
def test_wait_timeout_preserves_rounding_and_native_uint_saturation(timeout, expected):
    assert module._timeout_milliseconds(timeout) == expected
