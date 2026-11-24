"""Run a fixed argv in the active Windows console user's session.

The installed Agent normally runs as LocalSystem.  Processes started directly
from that account do not inherit the interactive user's WSL registration or
profile.  This module provides the narrow process primitive needed by those
callers: no command shell, no command text supplied by a controller, and no
credential handling.  A non-System Windows Agent simply uses ``subprocess``.
"""

from __future__ import annotations

import ctypes
import locale
import math
import os
import platform
import subprocess
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Sequence


_INVALID_SESSION_ID = 0xFFFFFFFF
_TOKEN_QUERY = 0x0008
_MAXIMUM_ALLOWED = 0x02000000
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_TOKEN_USER = 1
_WIN_LOCAL_SYSTEM_SID = 22

_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_NO_WINDOW = 0x08000000
_STARTF_USESHOWWINDOW = 0x00000001
_STARTF_USESTDHANDLES = 0x00000100
_SW_HIDE = 0

# ProcThreadAttributeValue(2, FALSE, TRUE, FALSE).  With
# EXTENDED_STARTUPINFO_PRESENT this restricts inheritance to the explicit
# standard-I/O handles below instead of every inheritable handle held by the
# long-running LocalSystem Agent.
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002

_DUPLICATE_SAME_ACCESS = 0x00000002
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF


class WindowsActiveUserError(OSError):
    """The active Windows user's process could not be created safely."""


@dataclass(frozen=True)
class ActiveUserIdentity:
    """Stable identity bound to one active console launch context."""

    session_id: int
    user_sid: str


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", _STARTUPINFOW),
        ("lpAttributeList", wintypes.LPVOID),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]


class _TOKEN_USER_VALUE(ctypes.Structure):
    _fields_ = [("User", _SID_AND_ATTRIBUTES)]


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _argv_strings(argv: Sequence[str | os.PathLike[str]]) -> list[str]:
    if isinstance(argv, (str, bytes, os.PathLike)):
        raise TypeError("argv must be a sequence of arguments, not command text")
    values = [os.fspath(value) for value in argv]
    if not values or not values[0]:
        raise ValueError("argv must contain a non-empty executable")
    if any(not isinstance(value, str) for value in values):
        raise TypeError("every argv item must be text or a text path")
    if any("\x00" in value for value in values):
        raise ValueError("argv must not contain NUL characters")
    return values


def _cwd_string(cwd: str | os.PathLike[str] | None) -> str | None:
    if cwd is None:
        return None
    value = os.fspath(cwd)
    if not isinstance(value, str):
        raise TypeError("cwd must be a text path")
    if "\x00" in value:
        raise ValueError("cwd must not contain NUL characters")
    return value


def _command_line(argv: Sequence[str]) -> str:
    """Quote argv with Python's canonical Windows command-line algorithm."""

    return subprocess.list2cmdline(list(argv))


def _timeout_milliseconds(timeout: float | None) -> int:
    if timeout is None:
        return _INFINITE
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("timeout must be a finite non-negative number or None")
    # Leave 0xffffffff reserved for INFINITE.
    return int(math.ceil(min(seconds, (_INFINITE - 1) / 1000.0) * 1000.0))


class _WindowsApi:
    """Small ctypes boundary, initialized only on Windows."""

    def __init__(self) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self._set_signatures()

    def _set_signatures(self) -> None:
        self.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        self.kernel32.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        self.kernel32.DuplicateHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.kernel32.DuplicateHandle.restype = wintypes.BOOL
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel32.TerminateProcess.restype = wintypes.BOOL
        self.kernel32.InitializeProcThreadAttributeList.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self.kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        self.kernel32.UpdateProcThreadAttribute.argtypes = [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        self.kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        self.kernel32.DeleteProcThreadAttributeList.argtypes = [wintypes.LPVOID]
        self.kernel32.DeleteProcThreadAttributeList.restype = None

        self.advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.OpenProcessToken.restype = wintypes.BOOL
        self.advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.GetTokenInformation.restype = wintypes.BOOL
        self.advapi32.IsWellKnownSid.argtypes = [wintypes.LPVOID, ctypes.c_int]
        self.advapi32.IsWellKnownSid.restype = wintypes.BOOL
        self.advapi32.ConvertSidToStringSidW.argtypes = [
            wintypes.LPVOID,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.advapi32.DuplicateTokenEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi32.DuplicateTokenEx.restype = wintypes.BOOL
        self.advapi32.CreateProcessAsUserW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            ctypes.POINTER(_SECURITY_ATTRIBUTES),
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            ctypes.POINTER(_STARTUPINFOW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        self.advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

        self.wtsapi32.WTSQueryUserToken.argtypes = [
            wintypes.ULONG,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        self.wtsapi32.WTSQueryUserToken.restype = wintypes.BOOL
        self.userenv.CreateEnvironmentBlock.argtypes = [
            ctypes.POINTER(wintypes.LPVOID),
            wintypes.HANDLE,
            wintypes.BOOL,
        ]
        self.userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
        self.userenv.DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
        self.userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL

    @staticmethod
    def error(action: str) -> WindowsActiveUserError:
        code = int(ctypes.get_last_error())
        detail = ctypes.FormatError(code).strip() or "unknown Windows error"
        error = WindowsActiveUserError(code, f"{action}: {detail}")
        error.winerror = code
        return error

    def close_handle(self, handle: int | None) -> None:
        if handle:
            self.kernel32.CloseHandle(wintypes.HANDLE(handle))

    def current_process(self) -> int:
        return int(self.kernel32.GetCurrentProcess())

    def wait(self, process: int, milliseconds: int) -> int:
        return int(
            self.kernel32.WaitForSingleObject(
                wintypes.HANDLE(process), wintypes.DWORD(milliseconds)
            )
        )

    def exit_code(self, process: int) -> int:
        value = wintypes.DWORD()
        if not self.kernel32.GetExitCodeProcess(
            wintypes.HANDLE(process), ctypes.byref(value)
        ):
            raise self.error("GetExitCodeProcess failed")
        return int(value.value)

    def terminate(self, process: int, exit_code: int) -> None:
        if not self.kernel32.TerminateProcess(
            wintypes.HANDLE(process), wintypes.UINT(exit_code)
        ):
            raise self.error("TerminateProcess failed")


_API: _WindowsApi | None = None


def _get_api() -> _WindowsApi:
    global _API
    if not _is_windows():
        raise WindowsActiveUserError("active-user process launch is Windows-only")
    if _API is None:
        _API = _WindowsApi()
    return _API


def _running_as_local_system() -> bool:
    """Identify LocalSystem by SID instead of a localized account name."""

    api = _get_api()
    token = wintypes.HANDLE()
    if not api.advapi32.OpenProcessToken(
        wintypes.HANDLE(api.current_process()), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise api.error("OpenProcessToken failed while identifying the Agent account")
    try:
        required = wintypes.DWORD()
        api.advapi32.GetTokenInformation(
            token, _TOKEN_USER, None, 0, ctypes.byref(required)
        )
        if not required.value:
            raise api.error("GetTokenInformation failed while identifying the Agent account")
        buffer = ctypes.create_string_buffer(required.value)
        if not api.advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            raise api.error("GetTokenInformation failed while identifying the Agent account")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_VALUE)).contents
        return bool(
            api.advapi32.IsWellKnownSid(
                token_user.User.Sid, _WIN_LOCAL_SYSTEM_SID
            )
        )
    finally:
        api.close_handle(int(token.value or 0))


def _token_user_sid(api: _WindowsApi, token: wintypes.HANDLE) -> str:
    required = wintypes.DWORD()
    api.advapi32.GetTokenInformation(
        token, _TOKEN_USER, None, 0, ctypes.byref(required)
    )
    if not required.value:
        raise api.error("GetTokenInformation failed for the active user")
    buffer = ctypes.create_string_buffer(required.value)
    if not api.advapi32.GetTokenInformation(
        token,
        _TOKEN_USER,
        buffer,
        required.value,
        ctypes.byref(required),
    ):
        raise api.error("GetTokenInformation failed for the active user")
    token_user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER_VALUE)).contents
    sid_pointer = wintypes.LPWSTR()
    if not api.advapi32.ConvertSidToStringSidW(
        token_user.User.Sid, ctypes.byref(sid_pointer)
    ):
        raise api.error("ConvertSidToStringSidW failed for the active user")
    try:
        sid = str(sid_pointer.value or "")
    finally:
        if sid_pointer:
            pointer_value = ctypes.cast(sid_pointer, ctypes.c_void_p).value
            api.kernel32.LocalFree(wintypes.HLOCAL(pointer_value))
    if not sid.startswith("S-") or len(sid) > 184:
        raise WindowsActiveUserError("active Windows user SID is invalid")
    return sid


def active_user_identity() -> ActiveUserIdentity:
    """Return the exact account/session used by active-user run and popen."""

    api = _get_api()
    token = wintypes.HANDLE()
    if _running_as_local_system():
        session_id = int(api.kernel32.WTSGetActiveConsoleSessionId())
        if session_id == _INVALID_SESSION_ID:
            raise WindowsActiveUserError(
                "no active Windows console user is signed in; WSL launch cannot continue"
            )
        if not api.wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(token)):
            raise api.error(
                f"WTSQueryUserToken failed for active console session {session_id}"
            )
    else:
        current_pid = int(api.kernel32.GetCurrentProcessId())
        session = wintypes.DWORD()
        if not api.kernel32.ProcessIdToSessionId(current_pid, ctypes.byref(session)):
            raise api.error("ProcessIdToSessionId failed for the current user")
        session_id = int(session.value)
        if not api.advapi32.OpenProcessToken(
            wintypes.HANDLE(api.current_process()), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise api.error("OpenProcessToken failed for the current user")
    try:
        return ActiveUserIdentity(session_id, _token_user_sid(api, token))
    finally:
        api.close_handle(int(token.value or 0))


def _native_file_handle(file: BinaryIO) -> int:
    try:
        descriptor = int(file.fileno())
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise WindowsActiveUserError("stdout must be an open local file") from exc
    try:
        import msvcrt

        handle = int(msvcrt.get_osfhandle(descriptor))
    except (ImportError, OSError) as exc:  # pragma: no cover - Windows runtime only
        raise WindowsActiveUserError("stdout does not expose a Windows file handle") from exc
    if handle == -1:
        raise WindowsActiveUserError("stdout exposes an invalid Windows file handle")
    return handle


def _duplicate_inheritable_handle(source: int) -> int:
    api = _get_api()
    duplicate = wintypes.HANDLE()
    current = wintypes.HANDLE(api.current_process())
    if not api.kernel32.DuplicateHandle(
        current,
        wintypes.HANDLE(source),
        current,
        ctypes.byref(duplicate),
        0,
        True,
        _DUPLICATE_SAME_ACCESS,
    ):
        raise api.error("DuplicateHandle failed for child output")
    return int(duplicate.value or 0)


def _open_inheritable_null_input() -> int:
    api = _get_api()
    security = _SECURITY_ATTRIBUTES(
        ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True
    )
    handle = api.kernel32.CreateFileW(
        "NUL",
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        ctypes.byref(security),
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    value = int(handle or 0)
    if not value or value == ctypes.c_void_p(-1).value:
        raise api.error("CreateFileW failed for child input")
    return value


def _initialize_handle_list(
    api: _WindowsApi, handles: Sequence[int]
) -> tuple[Any, wintypes.LPVOID, Any]:
    """Create one STARTUPINFOEX attribute containing only ``handles``.

    The backing buffer and HANDLE array are returned as well as the native
    pointer so callers can keep both alive through ``CreateProcessAsUserW``.
    All supplied handles must already be inheritable, as required by the
    Windows process-creation contract.
    """

    unique_handles = tuple(dict.fromkeys(int(handle) for handle in handles))
    if not unique_handles or any(handle <= 0 for handle in unique_handles):
        raise WindowsActiveUserError("child handle list is invalid")

    required = ctypes.c_size_t()
    # The sizing call is documented to fail with ERROR_INSUFFICIENT_BUFFER;
    # only the returned non-zero size is meaningful here.
    api.kernel32.InitializeProcThreadAttributeList(
        None, 1, 0, ctypes.byref(required)
    )
    if required.value <= 0:
        raise api.error("InitializeProcThreadAttributeList sizing failed")

    storage = ctypes.create_string_buffer(required.value)
    attribute_list = ctypes.cast(storage, wintypes.LPVOID)
    if not api.kernel32.InitializeProcThreadAttributeList(
        attribute_list, 1, 0, ctypes.byref(required)
    ):
        raise api.error("InitializeProcThreadAttributeList failed")

    handle_array_type = wintypes.HANDLE * len(unique_handles)
    handle_array = handle_array_type(*unique_handles)
    if not api.kernel32.UpdateProcThreadAttribute(
        attribute_list,
        0,
        _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
        ctypes.cast(handle_array, wintypes.LPVOID),
        ctypes.sizeof(handle_array),
        None,
        None,
    ):
        api.kernel32.DeleteProcThreadAttributeList(attribute_list)
        raise api.error("UpdateProcThreadAttribute failed for child handles")
    return storage, attribute_list, handle_array


class ActiveUserProcess:
    """Minimal ``subprocess.Popen``-compatible active-user process handle."""

    def __init__(self, args: Sequence[str], pid: int, process_handle: int) -> None:
        self.args = list(args)
        self.pid = int(pid)
        self._handle = int(process_handle)
        self.returncode: int | None = None
        self._closed = False
        self._api = _get_api()

    def _wait_for_exit(self, timeout: float | None) -> int | None:
        if self.returncode is not None:
            return self.returncode
        self._ensure_open()
        result = self._api.wait(self._handle, _timeout_milliseconds(timeout))
        if result == _WAIT_TIMEOUT:
            return None
        if result == _WAIT_FAILED:
            raise self._api.error("WaitForSingleObject failed")
        if result != _WAIT_OBJECT_0:
            raise WindowsActiveUserError(
                f"WaitForSingleObject returned unexpected status 0x{result:08x}"
            )
        self.returncode = self._api.exit_code(self._handle)
        return self.returncode

    def poll(self) -> int | None:
        return self._wait_for_exit(0)

    def wait(self, timeout: float | None = None) -> int:
        returncode = self._wait_for_exit(timeout)
        if returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return returncode

    def terminate(self) -> None:
        if self.poll() is None:
            self._api.terminate(self._handle, 1)

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        if not self._closed:
            self._api.close_handle(self._handle)
            self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise WindowsActiveUserError("process handle is already closed")

    def __del__(self) -> None:  # pragma: no cover - deterministic callers use close
        try:
            self.close()
        except Exception:
            pass


def _spawn_as_active_user(
    argv: Sequence[str],
    *,
    stdin_handle: int,
    stdout_handle: int,
    stderr_handle: int,
    cwd: str | None,
) -> ActiveUserProcess:
    api = _get_api()
    session_id = int(api.kernel32.WTSGetActiveConsoleSessionId())
    if session_id == _INVALID_SESSION_ID:
        raise WindowsActiveUserError(
            "no active Windows console user is signed in; WSL launch cannot continue"
        )

    user_token = wintypes.HANDLE()
    primary_token = wintypes.HANDLE()
    environment = wintypes.LPVOID()
    process_info = _PROCESS_INFORMATION()
    environment_created = False
    attribute_list = wintypes.LPVOID()
    attribute_storage: Any = None
    inherited_handles: Any = None
    try:
        if not api.wtsapi32.WTSQueryUserToken(session_id, ctypes.byref(user_token)):
            raise api.error(
                f"WTSQueryUserToken failed for active console session {session_id}"
            )
        if not api.advapi32.DuplicateTokenEx(
            user_token,
            _MAXIMUM_ALLOWED,
            None,
            _SECURITY_IMPERSONATION,
            _TOKEN_PRIMARY,
            ctypes.byref(primary_token),
        ):
            raise api.error("DuplicateTokenEx failed for the active console user")
        if not api.userenv.CreateEnvironmentBlock(
            ctypes.byref(environment), primary_token, False
        ):
            raise api.error("CreateEnvironmentBlock failed for the active console user")
        environment_created = True

        (
            attribute_storage,
            attribute_list,
            inherited_handles,
        ) = _initialize_handle_list(
            api, (stdin_handle, stdout_handle, stderr_handle)
        )
        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        startup.StartupInfo.lpDesktop = "winsta0\\default"
        startup.StartupInfo.dwFlags = (
            _STARTF_USESTDHANDLES | _STARTF_USESHOWWINDOW
        )
        startup.StartupInfo.wShowWindow = _SW_HIDE
        startup.StartupInfo.hStdInput = wintypes.HANDLE(stdin_handle)
        startup.StartupInfo.hStdOutput = wintypes.HANDLE(stdout_handle)
        startup.StartupInfo.hStdError = wintypes.HANDLE(stderr_handle)
        startup.lpAttributeList = attribute_list
        command_line = ctypes.create_unicode_buffer(_command_line(argv))
        if not api.advapi32.CreateProcessAsUserW(
            primary_token,
            None,
            command_line,
            None,
            None,
            True,
            _CREATE_UNICODE_ENVIRONMENT
            | _CREATE_NEW_PROCESS_GROUP
            | _EXTENDED_STARTUPINFO_PRESENT
            | _CREATE_NO_WINDOW,
            environment,
            cwd,
            ctypes.cast(ctypes.byref(startup), ctypes.POINTER(_STARTUPINFOW)),
            ctypes.byref(process_info),
        ):
            raise api.error(
                "CreateProcessAsUserW failed in the active console user session"
            )
        process_handle = int(process_info.hProcess or 0)
        process_info.hProcess = None
        return ActiveUserProcess(argv, int(process_info.dwProcessId), process_handle)
    finally:
        if attribute_list:
            api.kernel32.DeleteProcThreadAttributeList(attribute_list)
        # Keep the ctypes backing objects alive through process creation and
        # DeleteProcThreadAttributeList; assigning them here also makes that
        # lifetime explicit to static analyzers.
        _ = (attribute_storage, inherited_handles)
        api.close_handle(int(process_info.hThread or 0))
        api.close_handle(int(process_info.hProcess or 0))
        if environment_created:
            api.userenv.DestroyEnvironmentBlock(environment)
        api.close_handle(int(primary_token.value or 0))
        api.close_handle(int(user_token.value or 0))


def _active_user_popen(
    argv: Sequence[str], *, stdout: BinaryIO, cwd: str | None
) -> ActiveUserProcess:
    api = _get_api()
    stdin_handle = 0
    output_handle = 0
    try:
        stdin_handle = _open_inheritable_null_input()
        output_handle = _duplicate_inheritable_handle(_native_file_handle(stdout))
        return _spawn_as_active_user(
            argv,
            stdin_handle=stdin_handle,
            stdout_handle=output_handle,
            stderr_handle=output_handle,
            cwd=cwd,
        )
    finally:
        api.close_handle(output_handle)
        api.close_handle(stdin_handle)


def _decode_output(value: bytes) -> str:
    # WSL/Linux tools emit UTF-8 regardless of the Windows service account's
    # active ANSI code page.  Decode that losslessly first; retain the system
    # codec only for native Windows output that is not valid UTF-8.
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return value.decode(locale.getpreferredencoding(False), errors="replace")


def _active_user_run(
    argv: Sequence[str], *, timeout: float | None, cwd: str | None
) -> subprocess.CompletedProcess[str]:
    process: ActiveUserProcess | None = None
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        api = _get_api()
        stdin_handle = 0
        stdout_handle = 0
        stderr_handle = 0
        try:
            stdin_handle = _open_inheritable_null_input()
            stdout_handle = _duplicate_inheritable_handle(
                _native_file_handle(stdout_file)
            )
            stderr_handle = _duplicate_inheritable_handle(
                _native_file_handle(stderr_file)
            )
            process = _spawn_as_active_user(
                argv,
                stdin_handle=stdin_handle,
                stdout_handle=stdout_handle,
                stderr_handle=stderr_handle,
                cwd=cwd,
            )
        finally:
            api.close_handle(stderr_handle)
            api.close_handle(stdout_handle)
            api.close_handle(stdin_handle)

        try:
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    # The process may have exited in the timeout/terminate race.
                    pass
                try:
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                stdout_file.seek(0)
                stderr_file.seek(0)
                raise subprocess.TimeoutExpired(
                    list(argv), timeout,
                    output=_decode_output(stdout_file.read()),
                    stderr=_decode_output(stderr_file.read()),
                ) from None
            else:
                stdout_file.seek(0)
                stderr_file.seek(0)
                return subprocess.CompletedProcess(
                    list(argv), returncode,
                    _decode_output(stdout_file.read()),
                    _decode_output(stderr_file.read()),
                )
        finally:
            process.close()


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    timeout: float | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run argv in the active console user context and capture text output."""

    args = _argv_strings(argv)
    working_directory = _cwd_string(cwd)
    if not _is_windows():
        raise WindowsActiveUserError("active-user process launch is Windows-only")
    if not _running_as_local_system():
        try:
            result = subprocess.run(
                args,
                cwd=working_directory,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=timeout,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except subprocess.TimeoutExpired as error:
            # Keep the same timeout and captured output contract as SYSTEM.
            if error.output is not None:
                error.output = _decode_output(error.output)
            if error.stderr is not None:
                error.stderr = _decode_output(error.stderr)
            raise
        result.stdout = _decode_output(result.stdout)
        result.stderr = _decode_output(result.stderr)
        return result
    return _active_user_run(args, timeout=timeout, cwd=working_directory)


def popen(
    argv: Sequence[str | os.PathLike[str]],
    *,
    stdout: BinaryIO,
    cwd: str | os.PathLike[str] | None = None,
) -> subprocess.Popen[Any] | ActiveUserProcess:
    """Start argv in the active console user context with combined file output."""

    args = _argv_strings(argv)
    working_directory = _cwd_string(cwd)
    if not _is_windows():
        raise WindowsActiveUserError("active-user process launch is Windows-only")
    if not _running_as_local_system():
        return subprocess.Popen(
            args,
            cwd=working_directory,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        )
    return _active_user_popen(args, stdout=stdout, cwd=working_directory)


__all__ = [
    "ActiveUserIdentity",
    "ActiveUserProcess",
    "WindowsActiveUserError",
    "active_user_identity",
    "popen",
    "run",
]
