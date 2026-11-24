from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest

import agent.wsl_mining as wsl_mining
from agent.windows_active_user import ActiveUserIdentity
from agent.wsl_mining import (
    WslLaunchIdentity,
    WslMiningError,
    ensure_wsl_package_read_access,
    prepare_wsl_gpu_runtime,
    stop_wsl_guest,
)


IDENTITY = ActiveUserIdentity(7, "S-1-5-21-1000")


def _identity_output() -> str:
    return '{"distribution":"Ubuntu-22.04","uid":1000}\n'


def _linux_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if len(normalized) >= 3 and normalized[1:3] == ":/":
        return f"/mnt/{normalized[0].lower()}/{normalized[3:]}"
    return normalized


def _write_package_marker(root: Path) -> str:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".rigpilot-package.json":
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
            files.append((relative, content))
    fingerprint = hashlib.sha256()
    total = 0
    for relative, content in files:
        encoded = relative.encode("utf-8")
        total += len(content)
        fingerprint.update(len(encoded).to_bytes(4, "big"))
        fingerprint.update(encoded)
        fingerprint.update(len(content).to_bytes(8, "big"))
        fingerprint.update(hashlib.sha256(content).digest())
    key = fingerprint.hexdigest()
    (root / ".rigpilot-package.json").write_text(
        json.dumps(
            {
                "tree_sha256": key,
                "file_count": len(files),
                "extracted_bytes": total,
            }
        ),
        encoding="utf-8",
    )
    return key


def _cache_result(values: list[str]) -> subprocess.CompletedProcess[str] | None:
    if wsl_mining._GUEST_CACHE_PACKAGE not in values:
        return None
    position = values.index(wsl_mining._GUEST_CACHE_PACKAGE)
    relative = values[position + 2]
    key = values[position + 3]
    target = f"/home/user/.rigpilot/miner-cache-v1/{key}/{relative}"
    return subprocess.CompletedProcess(
        values,
        0,
        json.dumps({"target": target, "cwd": str(PurePosixPath(target).parent)}),
        "",
    )


def test_prepare_wsl_runtime_uses_fixed_probes_and_no_shell(tmp_path, monkeypatch):
    executable = tmp_path / "package" / "miner-cuda13"
    executable.parent.mkdir()
    executable.write_bytes(b"ELF")
    (executable.parent / "libpearl.so").write_bytes(b"RESOURCE")
    key = _write_package_marker(executable.parent)
    wsl = Path("C:/Windows/System32/wsl.exe")
    monkeypatch.setattr(wsl_mining, "_windows_system_executable", lambda _name: wsl)
    calls: list[list[str]] = []

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        calls.append(values)
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        if "/usr/bin/wslpath" in values:
            return subprocess.CompletedProcess(
                values, 0, _linux_path(values[-1]) + "\n", ""
            )
        cached = _cache_result(values)
        if cached is not None:
            return cached
        return subprocess.CompletedProcess(values, 0, "GPU 0: NVIDIA\n", "")

    launched: list[list[str]] = []

    def popen(argv, *, stdout):
        launched.append([str(item) for item in argv])
        return object()

    runtime = prepare_wsl_gpu_runtime(
        executable,
        package_root=executable.parent,
        system_name="Windows",
        runner=runner,
        popen_factory=popen,
        identity_factory=lambda: IDENTITY,
    )
    command = runtime.command([executable, "--flag"])

    assert command[:6] == [
        str(wsl),
        "--distribution",
        "Ubuntu-22.04",
        "--cd",
        f"/home/user/.rigpilot/miner-cache-v1/{key}",
        "--exec",
    ]
    assert command[6] == (
        f"/home/user/.rigpilot/miner-cache-v1/{key}/{executable.name}"
    )
    assert command[-1] == "--flag"
    assert any("/dev/dxg" in call for call in calls)
    assert any("/usr/lib/wsl/lib/nvidia-smi" in call for call in calls)
    assert not any("/usr/bin/chmod" in call for call in calls)
    assert all("bash" not in call and "sh" not in call for call in calls)
    cache_calls = [call for call in calls if wsl_mining._GUEST_CACHE_PACKAGE in call]
    assert len(cache_calls) == 1
    cache_call = cache_calls[0]
    position = cache_call.index(wsl_mining._GUEST_CACHE_PACKAGE)
    assert cache_call[position + 1] == _linux_path(
        str(executable.parent.resolve())
    )
    assert cache_call[position + 2 : position + 5] == [
        "miner-cuda13",
        key,
        "2",
    ]
    assert _linux_path(str(executable.resolve())) not in command


def test_prepare_wsl_runtime_reports_missing_gpu_mapping(tmp_path, monkeypatch):
    executable = tmp_path / "miner-cuda12"
    executable.write_bytes(b"ELF")
    _write_package_marker(tmp_path)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda _name: Path("C:/Windows/System32/wsl.exe"),
    )

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        return subprocess.CompletedProcess(values, 1, "", "not found")

    with pytest.raises(WslMiningError, match="/dev/dxg"):
        prepare_wsl_gpu_runtime(
            executable,
            package_root=tmp_path,
            system_name="Windows",
            runner=runner,
            identity_factory=lambda: IDENTITY,
        )


def test_wsl_runtime_rejects_controller_style_command_text(tmp_path, monkeypatch):
    executable = tmp_path / "miner-cuda12"
    executable.write_bytes(b"ELF")
    _write_package_marker(tmp_path)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda _name: Path("C:/Windows/System32/wsl.exe"),
    )

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        if "/usr/bin/wslpath" in values:
            return subprocess.CompletedProcess(
                values, 0, _linux_path(values[-1]) + "\n", ""
            )
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        cached = _cache_result(values)
        if cached is not None:
            return cached
        return subprocess.CompletedProcess(values, 0, "ok", "")

    runtime = prepare_wsl_gpu_runtime(
        executable,
        package_root=tmp_path,
        system_name="Windows",
        runner=runner,
        identity_factory=lambda: IDENTITY,
    )
    with pytest.raises(WslMiningError, match="verified executable"):
        runtime.command(["bash -c miner", "--unsafe"])
    with pytest.raises(WslMiningError, match="unsupported Windows path"):
        runtime.command([executable, "--worker", r"C:\Users\attacker\secret"])


def test_wsl_supervisor_and_srb_discovery_are_fixed_argv_only(tmp_path, monkeypatch):
    executable = tmp_path / "SRBMiner-MULTI"
    executable.write_bytes(b"ELF")
    _write_package_marker(tmp_path)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda _name: Path("C:/Windows/System32/wsl.exe"),
    )
    calls = []

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        calls.append(values)
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        if "/usr/bin/wslpath" in values:
            return subprocess.CompletedProcess(
                values, 0, _linux_path(values[-1]) + "\n", ""
            )
        cached = _cache_result(values)
        if cached is not None:
            return cached
        return subprocess.CompletedProcess(values, 0, "GPU0 : NVIDIA RTX 5080\n", "")

    runtime = prepare_wsl_gpu_runtime(
        executable,
        package_root=tmp_path,
        system_name="Windows",
        runner=runner,
        identity_factory=lambda: IDENTITY,
    )
    token = "a" * 64
    command = runtime.supervised_command(
        [executable, "--algorithm", "pearlhash"], token=token
    )
    assert command[:4] == [
        str(runtime.wsl_executable),
        "--distribution",
        "Ubuntu-22.04",
        "--cd",
    ]
    assert "/usr/bin/python3" in command
    supervisor = command.index(wsl_mining._GUEST_SUPERVISOR)
    assert command[supervisor + 3 : supervisor + 5] == ["1000", "Ubuntu-22.04"]
    completed = runtime.run_linux([executable, "--list-devices"], timeout=15)
    assert completed.returncode == 0
    assert calls[-1][calls[-1].index("-c") + 1] == wsl_mining._GUEST_PTY_DISCOVERY
    with pytest.raises(WslMiningError, match="discovery command"):
        runtime.run_linux([executable, "--algorithm", "pearlhash"])


def test_wsl_package_acl_grants_only_builtin_users_read_execute(
    tmp_path, monkeypatch
):
    packages = tmp_path / "packages"
    package = packages / "package"
    package.mkdir(parents=True)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda name: Path(f"C:/Windows/System32/{name}"),
    )
    calls = []

    def runner(argv, **kwargs):
        calls.append(([str(item) for item in argv], kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    ensure_wsl_package_read_access(
        package,
        packages_root=packages,
        runner=runner,
    )

    assert len(calls) == 2
    parent_argv, parent_kwargs = calls[0]
    package_argv, package_kwargs = calls[1]
    assert parent_argv[0].endswith("/icacls.exe") or parent_argv[0].endswith(
        "\\icacls.exe"
    )
    assert parent_argv[1] == str(packages.resolve())
    assert "*S-1-5-32-545:(RX)" in parent_argv
    assert "/T" not in parent_argv
    assert not any("(OI)" in item or "(CI)" in item for item in parent_argv)
    assert package_argv[1] == str(package.resolve())
    assert "*S-1-5-32-545:(OI)(CI)(RX)" in package_argv
    assert "/T" in package_argv
    assert not any(
        "(M)" in item or "(F)" in item
        for argv, _kwargs in calls
        for item in argv
    )
    assert parent_kwargs["shell"] is False
    assert package_kwargs["shell"] is False


@pytest.mark.parametrize("location", ["same", "nested", "outside"])
def test_wsl_package_acl_rejects_non_direct_package_root(
    tmp_path, monkeypatch, location
):
    packages = tmp_path / "packages"
    packages.mkdir()
    if location == "same":
        package = packages
    elif location == "nested":
        package = packages / "group" / "package"
        package.mkdir(parents=True)
    else:
        package = tmp_path / "outside"
        package.mkdir()
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda name: Path(f"C:/Windows/System32/{name}"),
    )
    calls = []

    with pytest.raises(WslMiningError, match="direct child"):
        ensure_wsl_package_read_access(
            package,
            packages_root=packages,
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

    assert calls == []


def test_stop_wsl_guest_uses_bound_distribution_and_confirms_absence():
    identity = WslLaunchIdentity(
        7, "S-1-5-21-1000", "Ubuntu-22.04", 1000
    )
    calls = []

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        calls.append(values)
        operation = values[values.index(wsl_mining._GUEST_CONTROL) + 1]
        return subprocess.CompletedProcess(
            values, 0 if operation == "stop" else 3, "", ""
        )

    stopped = stop_wsl_guest(
        Path("C:/Windows/System32/wsl.exe"),
        "b" * 64,
        "/mnt/c/package/miner-cuda13",
        identity,
        runner=runner,
        identity_factory=lambda: IDENTITY,
    )

    assert stopped is True
    assert len(calls) == 2
    assert all(
        call[1:3] == ["--distribution", "Ubuntu-22.04"] for call in calls
    )
    assert all("--terminate" not in call and "killall" not in call for call in calls)


def test_wsl_cache_rejects_invalid_install_marker_before_running_wsl(
    tmp_path, monkeypatch
):
    executable = tmp_path / "package" / "miner-cuda13"
    executable.parent.mkdir()
    executable.write_bytes(b"ELF")
    (executable.parent / ".rigpilot-package.json").write_text(
        json.dumps(
            {
                "tree_sha256": "../unsafe",
                "file_count": 1,
                "extracted_bytes": 3,
            }
        ),
        encoding="utf-8",
    )
    called = False

    def runner(_argv, *, timeout=None):
        nonlocal called
        called = True
        raise AssertionError("WSL must not run for an invalid marker")

    with pytest.raises(WslMiningError, match="marker has invalid cache metadata"):
        prepare_wsl_gpu_runtime(
            executable,
            package_root=executable.parent,
            system_name="Windows",
            runner=runner,
            identity_factory=lambda: IDENTITY,
        )
    assert called is False


def test_wsl_cache_rejects_helper_path_outside_fixed_boundary(tmp_path, monkeypatch):
    executable = tmp_path / "package" / "miner-cuda13"
    executable.parent.mkdir()
    executable.write_bytes(b"ELF")
    _write_package_marker(executable.parent)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda _name: Path("C:/Windows/System32/wsl.exe"),
    )

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        if "/usr/bin/wslpath" in values:
            return subprocess.CompletedProcess(
                values, 0, _linux_path(values[-1]) + "\n", ""
            )
        if wsl_mining._GUEST_CACHE_PACKAGE in values:
            return subprocess.CompletedProcess(
                values,
                0,
                json.dumps({"target": "/tmp/miner", "cwd": "/tmp"}),
                "",
            )
        return subprocess.CompletedProcess(values, 0, "GPU 0: NVIDIA\n", "")

    with pytest.raises(WslMiningError, match="escaped its fixed boundary"):
        prepare_wsl_gpu_runtime(
            executable,
            package_root=executable.parent,
            system_name="Windows",
            runner=runner,
            identity_factory=lambda: IDENTITY,
        )


def test_wsl_cache_copy_failure_has_no_drvfs_execution_fallback(
    tmp_path, monkeypatch
):
    executable = tmp_path / "package" / "SRBMiner-MULTI"
    executable.parent.mkdir()
    executable.write_bytes(b"ELF")
    _write_package_marker(executable.parent)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda _name: Path("C:/Windows/System32/wsl.exe"),
    )
    calls: list[list[str]] = []

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        calls.append(values)
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        if "/usr/bin/wslpath" in values:
            return subprocess.CompletedProcess(
                values, 0, _linux_path(values[-1]) + "\n", ""
            )
        if wsl_mining._GUEST_CACHE_PACKAGE in values:
            return subprocess.CompletedProcess(
                values,
                66,
                "sensitive stdout",
                "copy failed secret-wallet /mnt/c/private",
            )
        return subprocess.CompletedProcess(values, 0, "GPU 0: NVIDIA\n", "")

    with pytest.raises(WslMiningError) as captured:
        prepare_wsl_gpu_runtime(
            executable,
            package_root=executable.parent,
            system_name="Windows",
            runner=runner,
            identity_factory=lambda: IDENTITY,
        )
    assert str(captured.value).endswith("private Linux cache (exit 66)")
    assert "copy failed" not in str(captured.value)
    assert "secret-wallet" not in str(captured.value)
    assert "/mnt/c/private" not in str(captured.value)
    assert "sensitive stdout" not in str(captured.value)
    source_target = _linux_path(str(executable.resolve()))
    assert not any(
        "/usr/bin/test" in call and source_target in call for call in calls
    )


def test_wsl_cache_key_is_reusable_when_switching_pearl_cuda_target(
    tmp_path, monkeypatch
):
    package = tmp_path / "package"
    package.mkdir()
    cuda12 = package / "miner-cuda12"
    cuda13 = package / "miner-cuda13"
    cuda12.write_bytes(b"CUDA12")
    cuda13.write_bytes(b"CUDA13")
    key = _write_package_marker(package)
    monkeypatch.setattr(
        wsl_mining,
        "_windows_system_executable",
        lambda _name: Path("C:/Windows/System32/wsl.exe"),
    )
    cache_calls: list[list[str]] = []

    def runner(argv, *, timeout=None):
        values = [str(item) for item in argv]
        if wsl_mining._GUEST_IDENTITY in values:
            return subprocess.CompletedProcess(values, 0, _identity_output(), "")
        if "/usr/bin/wslpath" in values:
            return subprocess.CompletedProcess(
                values, 0, _linux_path(values[-1]) + "\n", ""
            )
        cached = _cache_result(values)
        if cached is not None:
            cache_calls.append(values)
            return cached
        return subprocess.CompletedProcess(values, 0, "GPU 0: NVIDIA\n", "")

    first = prepare_wsl_gpu_runtime(
        cuda12,
        package_root=package,
        system_name="Windows",
        runner=runner,
        identity_factory=lambda: IDENTITY,
    )
    second = prepare_wsl_gpu_runtime(
        cuda13,
        package_root=package,
        system_name="Windows",
        runner=runner,
        identity_factory=lambda: IDENTITY,
    )

    assert first.cache_key == second.cache_key == key
    assert first.linux_executable.endswith(f"/{key}/miner-cuda12")
    assert second.linux_executable.endswith(f"/{key}/miner-cuda13")
    assert len(cache_calls) == 2
    assert all(key in call for call in cache_calls)


@pytest.mark.skipif(os.name != "posix", reason="guest cache filesystem proof requires POSIX permissions")
@pytest.mark.parametrize("tamper", ["content", "root-link", "marker-link", "executable-link", "resource-link"])
def test_guest_cache_copies_complete_bundle_and_reuses_it_across_executable_choices(tmp_path, tamper):
    import builtins
    import io
    import re
    import shutil
    import stat
    import tempfile
    from contextlib import redirect_stdout
    from types import SimpleNamespace
    from server.miner_adapters import MINER_ADAPTERS

    names = sorted({name for adapter in MINER_ADAPTERS.list() if "wsl_gpu" in adapter.capabilities
                    for name in adapter.executable_candidates("Linux")})
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    payloads = {name: ("synthetic " + name).encode() for name in names}
    payloads["resources/libexample.so"] = b"sibling resource"
    for name, content in payloads.items():
        path = source / name
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(content)
    key = _write_package_marker(source)
    home = tmp_path / "guest-home"
    home.mkdir(mode=0o700)
    local_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    local_os.environ = {"WSL_DISTRO_NAME": "Fixture"}
    bundle = home / ".rigpilot/miner-cache-v1" / key
    opened = []
    scans = []
    read_bytes = []
    descriptors = {}

    def observed_open(path, flags, *args):
        fd = os.open(path, flags, *args)
        if not flags & (os.O_WRONLY | os.O_RDWR):
            opened.append(str(path))
            descriptors[fd] = str(path)
        return fd

    def observed_read(fd, count):
        content = os.read(fd, count)
        if fd in descriptors:
            read_bytes.append((descriptors[fd], len(content)))
        return content

    def observed_close(fd):
        descriptors.pop(fd, None)
        os.close(fd)

    def observed_scandir(path):
        scans.append(str(path))
        return os.scandir(path)

    local_os.open = observed_open
    local_os.read = observed_read
    local_os.close = observed_close
    local_os.scandir = observed_scandir
    modules = {"hashlib": hashlib, "json": json, "os": local_os, "re": re,
               "shutil": shutil, "stat": stat, "tempfile": tempfile,
               "pwd": SimpleNamespace(getpwuid=lambda uid: SimpleNamespace(pw_dir=str(home)))}
    source_before = {name: (source / name).read_bytes() for name in payloads}

    def run(target):
        modules["sys"] = SimpleNamespace(argv=["helper", str(source), target, key,
            str(len(payloads)), str(sum(map(len, payloads.values()))), str(os.getuid()), "Fixture"])
        fixture_builtins = dict(vars(builtins), __import__=lambda name, *_a, **_kw: modules[name])
        output = io.StringIO()
        with redirect_stdout(output):
            exec(wsl_mining._GUEST_CACHE_PACKAGE, {"__builtins__": fixture_builtins})
        return json.loads(output.getvalue())

    for index, name in enumerate(names):
        opened.clear()
        scans.clear()
        read_bytes.clear()
        returned = run(name)
        if index:
            # A warm hit still hashes every payload byte once, without a
            # second cache scan or reading the DrvFS/source package again.
            assert scans.count(str(bundle)) == 1
            assert sorted(opened) == sorted(str(bundle / item) for item in payloads)
            assert sum(count for _, count in read_bytes) == sum(map(len, payloads.values()))
            assert not descriptors
        assert returned == {"target": str(bundle / name), "cwd": str(bundle)}
        assert {p.name for p in bundle.parent.iterdir()} == {key}
        assert {item: (bundle / item).read_bytes() for item in payloads} == payloads
        for item in payloads:
            assert (source / item).stat().st_ino != (bundle / item).stat().st_ino
            assert bool((bundle / item).stat().st_mode & 0o111) is (item in names)
    assert source_before == {name: (source / name).read_bytes() for name in payloads}
    external = None
    if tamper == "content":
        changed = bundle / "resources/libexample.so"
        changed.chmod(0o600)
        changed.write_bytes(b"tampered runtime resource")
    elif tamper == "root-link":
        external = bundle.with_name(key + "-retained")
        bundle.rename(external)
        bundle.symlink_to(external, target_is_directory=True)
    else:
        changed = bundle / ({"marker-link": ".rigpilot-cache.json",
                             "executable-link": names[0],
                             "resource-link": "resources/libexample.so"}[tamper])
        external = home / "external-payload"
        external.write_bytes(changed.read_bytes())
        external.chmod(changed.stat().st_mode & 0o777)
        changed.unlink()
        changed.symlink_to(external)
    external_before = (external.read_bytes() if external is not None and external.is_file() else
                       {item: (external / item).read_bytes() for item in payloads} if external is not None else None)
    with pytest.raises(SystemExit) as error:
        run(names[0])
    assert error.value.code == 65
    assert (source / "resources/libexample.so").read_bytes() == payloads["resources/libexample.so"]
    if external is not None:
        assert (external.read_bytes() if external.is_file() else
                {item: (external / item).read_bytes() for item in payloads}) == external_before


@pytest.mark.parametrize("supervisor_alive", [False, True])
@pytest.mark.parametrize("changed_identity", [False, True])
def test_guest_control_signals_only_the_bound_child_group(supervisor_alive, changed_identity):
    import builtins
    import io
    import re
    from types import SimpleNamespace

    token = "a" * 64
    record = {"version": 1, "token": token, "supervisor_pid": 99, "supervisor_start": 2000,
              "child_pid": 1234, "child_start": 3000, "target": "/owned/miner"}
    child_alive = [True]
    signals = []

    def open_fixture(path, *_args, **_kwargs):
        if path == "/tmp/rigpilot-miner-" + token + ".json":
            return io.StringIO(json.dumps(record))
        if path == "/proc/1234/cmdline":
            return io.BytesIO(b"/owned/miner\0--flag\0")
        if path not in {"/proc/99/stat", "/proc/1234/stat"}:
            pytest.fail("unapproved process or file inspected: " + path)
        supervisor = path == "/proc/99/stat"
        if (supervisor and not supervisor_alive) or (not supervisor and not child_alive[0]):
            raise FileNotFoundError(path)
        fields = ["0"] * 20
        fields[1] = "99" if supervisor_alive else "1"
        fields[3] = "1234"
        fields[19] = "2000" if supervisor else "3001" if changed_identity else "3000"
        return io.BytesIO(("1234 (owned) " + " ".join(fields)).encode())

    def signal(kind, pid, number):
        signals.append((kind, pid, number))
        child_alive[0] = False

    local_os = SimpleNamespace(environ={"WSL_DISTRO_NAME": "Fixture"}, getuid=lambda: 1000,
        path=SimpleNamespace(isabs=lambda value: value.startswith("/"), islink=lambda value: False),
        lstat=lambda path: SimpleNamespace(st_mode=0o100600, st_uid=1000),
        fsdecode=lambda value: value.decode(),
        kill=lambda pid, number: signal("supervisor", pid, number),
        killpg=lambda pid, number: signal("group", pid, number))
    modules = {"json": json, "os": local_os, "re": re,
        "signal": SimpleNamespace(SIGTERM=15, SIGKILL=9),
        "sys": SimpleNamespace(argv=["helper", "stop", token, "/owned/miner", "1000", "Fixture"]),
        "time": SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda value: pytest.fail("bound child already exited"))}
    fixture_builtins = dict(vars(builtins), open=open_fixture,
                           __import__=lambda name, *_a, **_kw: modules[name])
    with pytest.raises(SystemExit) as error:
        exec(wsl_mining._GUEST_CONTROL, {"__builtins__": fixture_builtins})
    assert error.value.code == (3 if changed_identity else 0)
    assert signals == ([] if changed_identity else
                       [("supervisor", 99, 15)] if supervisor_alive else [("group", 1234, 15)])


@pytest.mark.parametrize("failure", [
    "write", "fsync", "zero_write", "term_timeout", "cleanup_unconfirmed",
    "short_write", "normal_exit",
])
def test_guest_supervisor_publication_failure_reaps_owned_child(failure):
    # Execute the real embedded program with all process/filesystem primitives
    # replaced. No WSL, child process, root service or miner is started.
    import builtins
    import io
    import re
    from types import SimpleNamespace

    events = []
    published = bytearray()
    child = SimpleNamespace(pid=1234, alive=True)
    child.poll = lambda: None if child.alive else 0
    def wait(timeout=None):
        events.append(("wait", timeout))
        if child.alive:
            raise subprocess.TimeoutExpired("owned-child", timeout)
        return 0
    child.wait = wait
    def popen(argv, **kwargs):
        assert argv[0] == "/owned/miner"
        assert kwargs["start_new_session"] is True
        assert kwargs["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
        events.append(("spawn", child.pid))
        return child
    def killpg(pid, signal_number):
        assert pid == child.pid
        events.append(("killpg", pid, signal_number))
        if failure != "cleanup_unconfirmed" and (failure != "term_timeout" or signal_number == 9):
            child.alive = False
    def write(_fd, value):
        if failure == "write":
            raise OSError("record write failed")
        if failure == "zero_write":
            return 0
        written = min(7, len(value)) if failure == "short_write" else len(value)
        published.extend(value[:written])
        return written
    def fsync(_fd):
        if failure in {"fsync", "term_timeout", "cleanup_unconfirmed"}:
            raise OSError("record fsync failed")
        child.alive = False
    fake_os = SimpleNamespace(
        environ={"WSL_DISTRO_NAME": "Test"}, getuid=lambda: 1000,
        getpid=lambda: 99, open=lambda *_a: 10,
        fstat=lambda _fd: SimpleNamespace(st_ino=42), write=write, fsync=fsync,
        close=lambda fd: events.append(("close", fd)),
        lstat=lambda _p: SimpleNamespace(st_ino=42),
        unlink=lambda _p: events.append(("unlink",)), killpg=killpg,
        path=SimpleNamespace(isabs=lambda p: p.startswith("/"), islink=lambda _p: False),
        O_WRONLY=1, O_CREAT=2, O_EXCL=4, O_NOFOLLOW=8,
    )
    modules = {
        "json": json, "re": re, "os": fake_os,
        "signal": SimpleNamespace(SIGTERM=15, SIGINT=2, SIGKILL=9, signal=lambda *_a: None),
        "subprocess": SimpleNamespace(Popen=popen, DEVNULL=-3, TimeoutExpired=subprocess.TimeoutExpired),
        "sys": SimpleNamespace(argv=["helper", "a" * 64, "/owned", "1000", "Test", "/owned/miner"]),
        "time": SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _delay: None),
    }
    def import_mock(name, *_args, **_kwargs):
        return modules[name]
    def proc_stat(_path, *_args):
        fields = ["0"] * 20
        fields[19] = "2000"
        return io.BytesIO(("99 (owned) " + " ".join(fields)).encode())
    fixture_builtins = dict(vars(builtins), __import__=import_mock, open=proc_stat)
    if failure in {"short_write", "normal_exit"}:
        with pytest.raises(SystemExit) as captured:
            exec(wsl_mining._GUEST_SUPERVISOR, {"__builtins__": fixture_builtins})
        assert captured.value.code == 0
        assert json.loads(published) == {
            "version": 1, "token": "a" * 64, "supervisor_pid": 99,
            "supervisor_start": 2000, "child_pid": child.pid,
            "child_start": 2000, "target": "/owned/miner",
        }
        assert not any(event[0] == "killpg" for event in events)
    else:
        with pytest.raises(OSError, match="record"):
            exec(wsl_mining._GUEST_SUPERVISOR, {"__builtins__": fixture_builtins})
        assert ("killpg", child.pid, 15) in events
    assert child.alive is (failure == "cleanup_unconfirmed")
    assert (("unlink",) in events) is (failure != "cleanup_unconfirmed")
    if failure in {"term_timeout", "cleanup_unconfirmed"}:
        assert ("killpg", child.pid, 9) in events
        assert ("wait", 10) in events and ("wait", 5) in events
    assert any(event[0] == "wait" for event in events)
