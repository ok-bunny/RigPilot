"""Isolated recovery tests for the Controller deployer's real Bash function.

These exercise the deployer's prepared-state program and real Bash recovery
functions with a temporary filesystem and modeled root ownership. They do not
execute deployment preflight, host locks, root operations, venv Python,
systemd, builds, HTTP, or a server/VM connection.
"""
from __future__ import annotations

import ast
import json
import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]


def _controller_source():
    return (ROOT / "deploy/deploy-controller-release.sh").read_text()


@pytest.mark.parametrize("mask", ["022", "077"])
def test_deployment_stage_is_private_independently_of_callers_umask(tmp_path, mask):
    source = _controller_source()
    command = next(line for line in source.splitlines()
                   if line.startswith("mkdir ") and line.endswith('"$stage"'))
    stage = tmp_path / "stage"
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", 'umask "$1"; stage=$2; ' + command,
         "fixture", mask, str(stage)],
        env={"PATH": os.defpath}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert stage.stat().st_mode & 0o777 == 0o700


def _health_port_prefix(source):
    return source[:source.index("for required in flock ")]


@pytest.mark.parametrize("port", [None, "9080"])
def test_health_check_uses_selected_loopback_port_without_real_http(tmp_path, port):
    source = _controller_source()
    prefix = _health_port_prefix(source)
    health = source[source.index("healthy=0\n"):source.index("committed_state=", source.index("healthy=0\n"))]
    log = tmp_path / "calls.jsonl"
    environment = {"PATH": os.defpath, "REVIEW_HEALTH_LOG": str(log)}
    if port is not None:
        environment["RIGPILOT_CONTROLLER_HEALTH_PORT"] = port
    mocks = r'''
curl() {
    printf '%s\n' "$*" >>"$REVIEW_HEALTH_LOG"
    printf '{"status":"ok","version":"1.2.3"}\n'
}
systemctl() { [[ "$*" == "is-active --quiet rigpilot-controller" ]]; }
rollback() { echo unexpected-rollback >&2; exit 99; }
journalctl() { echo unexpected-journalctl >&2; exit 98; }
'''
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, "1.2.3"],
        input=prefix + mocks + health, env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    selected = port or "8080"
    assert log.read_text().strip() == f"-fsS --max-time 2 http://127.0.0.1:{selected}/api/health"


@pytest.mark.parametrize("port", ["0", "65536", "-1", "08080", "8080/path", "example.invalid"])
def test_invalid_health_port_is_rejected_before_host_commands(port):
    prefix = _health_port_prefix(_controller_source())
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, "1.2.3"],
        input=prefix, env={"PATH": os.defpath, "RIGPILOT_CONTROLLER_HEALTH_PORT": port},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "health port must be an integer from 1 to 65535" in result.stderr


def test_selected_health_port_keeps_real_health_failure_rollback(tmp_path):
    source = _controller_source()
    health = source[source.index("healthy=0\n"):source.index("committed_state=", source.index("healthy=0\n"))]
    log = tmp_path / "health.log"
    mocks = r'''
seq() { printf '1\n'; }
sleep() { :; }
curl() {
    printf '%s\n' "$*" >>"$REVIEW_HEALTH_LOG"
    printf '{"status":"unhealthy","version":"1.2.3"}\n'
}
journalctl() { printf journal >>"$REVIEW_HEALTH_LOG"; }
rollback() { printf rollback >>"$REVIEW_HEALTH_LOG"; exit 97; }
systemctl() { echo unexpected-systemctl >&2; exit 98; }
'''
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, "1.2.3"],
        input=_health_port_prefix(source) + mocks + health,
        env={"PATH": os.defpath, "REVIEW_HEALTH_LOG": str(log),
             "RIGPILOT_CONTROLLER_HEALTH_PORT": "9080"},
        capture_output=True, text=True,
    )
    assert result.returncode == 97, result.stderr
    assert log.read_text() == "-fsS --max-time 2 http://127.0.0.1:9080/api/health\njournalrollback"


def _health_loop(source):
    start = source.index("healthy=0\n")
    return source[start:source.index("committed_state=", start)]


@pytest.mark.parametrize("window", [None, "", "240", "86400"])
def test_health_window_bounds_reach_the_probe_loop_and_default_to_30(tmp_path, window):
    source = _controller_source()
    log = tmp_path / "health.log"
    environment = {"PATH": os.defpath, "REVIEW_HEALTH_LOG": str(log)}
    if window is not None:
        environment["RIGPILOT_HEALTH_WAIT_SECONDS"] = window
    mocks = r'''
seq() { printf '%s %s\n' "$1" "$2" >>"$REVIEW_HEALTH_LOG"; printf '1\n'; }
sleep() { :; }
curl() { printf '{"status":"ok","version":"1.2.3"}\n'; }
systemctl() { [[ "$*" == "is-active --quiet rigpilot-controller" ]]; }
rollback() { echo unexpected-rollback >&2; exit 99; }
journalctl() { echo unexpected-journalctl >&2; exit 98; }
'''
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, "1.2.3"],
        input=_health_port_prefix(source) + mocks + _health_loop(source),
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == f"1 {window or '30'}\n"


@pytest.mark.parametrize("window,slow_probes,expected", [
    # A startup that stays unhealthy past the historical 30s window is
    # rolled back when the default applies (0.8.50 measured 89.68s).
    (None, 35, "rollback"),
    # The same slow startup deploys once the window is raised.
    ("240", 35, "healthy"),
])
def test_health_window_env_extends_the_wait_for_slow_controller(
    tmp_path, window, slow_probes, expected,
):
    source = _controller_source()
    log = tmp_path / "health.log"
    probes = tmp_path / "probe-count"
    environment = {"PATH": os.defpath, "REVIEW_HEALTH_LOG": str(log),
                   "REVIEW_HEALTH_COUNT": str(probes)}
    if window is not None:
        environment["RIGPILOT_HEALTH_WAIT_SECONDS"] = window
    mocks = r'''
seq() {
    printf 'bounds %s %s\n' "$1" "$2" >>"$REVIEW_HEALTH_LOG"
    count=0
    while (( count < $2 )); do count=$(( count + 1 )); printf '%d\n' "$count"; done
}
sleep() { printf 'sleep\n' >>"$REVIEW_HEALTH_LOG"; }
curl() {
    count=$(($(cat "$REVIEW_HEALTH_COUNT" 2>/dev/null || printf 0) + 1))
    printf '%s' "$count" >"$REVIEW_HEALTH_COUNT"
    if (( count < SLOW_PROBES )); then exit 22; fi
    printf '{"status":"ok","version":"1.2.3"}\n'
}
journalctl() { printf 'journal\n' >>"$REVIEW_HEALTH_LOG"; }
rollback() { printf 'rollback\n' >>"$REVIEW_HEALTH_LOG"; exit 97; }
systemctl() { [[ "$*" == "is-active --quiet rigpilot-controller" ]]; }
'''.replace("SLOW_PROBES", str(slow_probes))
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, "1.2.3"],
        input=_health_port_prefix(source) + mocks + _health_loop(source),
        env=environment, capture_output=True, text=True,
    )
    lines = log.read_text().splitlines()
    assert lines[0] == f"bounds 1 {window or '30'}"
    if expected == "healthy":
        assert result.returncode == 0, result.stderr
        assert probes.read_text() == str(slow_probes)
        assert lines[1:] == ["sleep"] * (slow_probes - 1)
    else:
        assert result.returncode == 97, result.stderr
        assert probes.read_text() == "30"
        assert lines[1:] == ["sleep"] * 30 + ["journal", "rollback"]


@pytest.mark.parametrize("window", ["0", "-1", "030", "30s", "1.5", "86401",
                                    "99999999999999999999999"])
def test_invalid_health_window_is_rejected_before_host_commands(window):
    prefix = _health_port_prefix(_controller_source())
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, "1.2.3"],
        input=prefix, env={"PATH": os.defpath, "RIGPILOT_HEALTH_WAIT_SECONDS": window},
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "health wait window must be an integer from 1 to 86400 seconds" in result.stderr


@pytest.mark.parametrize("version,accepted", [
    ("0.0.0", True), ("0.8.25", True), ("10.20.30", True),
    ("01.2.3", False), ("1.02.3", False), ("1.2.03", False),
])
def test_deployment_version_matches_current_builder_contract(version, accepted):
    source = _controller_source()
    start = source.index('[[ "$version" =~ ')
    end = source.index('\n[[ "$expected_sha256"', start)
    result = subprocess.run(
        ["/bin/bash", "-eu", "-s", "--", "prepare", "fixture", "a" * 64, version],
        input=_health_port_prefix(source) + source[start:end] + "\n",
        env={"PATH": os.defpath}, capture_output=True, text=True,
    )
    assert (result.returncode == 0) is accepted, result.stderr

    # Execute the actual staged-source version extraction, without reading any package.
    program = source[source.index('version_pattern = r"'):source.index("\nexpected_wheels =", source.index('version_pattern = r"'))]
    namespace = {"re": __import__("re"), "expected_version": version,
                 "version_source": f'__version__ = "{version}"\n'}
    if accepted:
        exec(program, namespace)
    else:
        with pytest.raises(RuntimeError, match="source version does not match"):
            exec(program, namespace)

    state_program = source.split("prepared_state_action() {", 1)[1].split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    validator = next(node for node in ast.parse(state_program).body
                     if isinstance(node, ast.FunctionDef) and node.name == "strict_state_dir")
    matcher = next(node for node in ast.walk(validator)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "fullmatch")
    state_name = f".controller-prepare-{version}-20261007T120000Z-123-" + "a" * 24
    assert (namespace["re"].fullmatch(ast.literal_eval(matcher.args[0]), state_name) is not None) is accepted


def _write_tree(path: Path, marker: str) -> None:
    path.mkdir(parents=True)
    (path / "identity").write_text(marker)


def _mock_commands(binary_dir: Path) -> None:
    binary_dir.mkdir()
    dispatcher = r'''import json
import os
from pathlib import Path
import shutil
import sys

root = Path(os.environ["REVIEW_INSTALL_ROOT"]).resolve()
command = Path(sys.argv[0]).name
arguments = sys.argv[1:]
with Path(os.environ["REVIEW_COMMAND_LOG"]).open("a") as output:
    output.write(json.dumps({"command": command, "arguments": arguments}) + "\n")
if os.environ.get("REVIEW_FAIL_COMMAND") == command:
    raise SystemExit("modeled command failure")


def bounded(argument):
    path = Path(os.path.abspath(argument))
    path.relative_to(root)
    path.parent.resolve().relative_to(root)
    return path


if command == "systemctl":
    if len(arguments) < 2 or arguments[-1] != "fixture-controller":
        raise SystemExit("unexpected systemd operation")
    service_state = os.environ.get("REVIEW_SERVICE_STATE")
    if arguments[0] == "is-active":
        active = os.environ.get("REVIEW_FAIL_COMMAND") == "active-service" or (
            service_state and Path(service_state).read_text() == "active"
        )
        raise SystemExit(0 if active else 3)
    if arguments[0] not in {"start", "stop"}:
        raise SystemExit("unexpected systemd operation")
    if service_state:
        Path(service_state).write_text("active" if arguments[0] == "start" else "inactive")
elif command == "realpath":
    path = bounded(arguments[-1]).resolve(strict=True)
    path.relative_to(root)
    print(path)
elif command == "mv":
    operands = [argument for argument in arguments if argument not in {"--", "-T"}]
    source, destination = map(bounded, operands)
    if destination.exists() or destination.is_symlink():
        raise SystemExit("fixture refuses an overwrite")
    os.rename(source, destination)
elif command == "ln":
    operands = [argument for argument in arguments if argument not in {"--", "-s"}]
    source, destination = map(bounded, operands)
    os.symlink(source, destination)
elif command == "rm":
    operands = [argument for argument in arguments if argument not in {"--", "-f", "-rf", "--one-file-system"}]
    for argument in operands:
        path = bounded(argument)
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
else:
    raise SystemExit("unexpected fixture command")
'''
    for name in ("systemctl", "realpath", "mv", "ln", "rm"):
        command = binary_dir / name
        command.write_text(f"#!{sys.executable}\n" + dispatcher)
        command.chmod(0o700)


def _run_recovery(tmp_path, install_root, values, recovery, action, *, service_state=None, failed_command=None):
    binary_dir = tmp_path / "mock-bin"
    command_log = tmp_path / "commands.jsonl"
    _mock_commands(binary_dir)
    environment = os.environ.copy()
    environment.update(
        PATH=str(binary_dir),
        REVIEW_INSTALL_ROOT=str(install_root),
        REVIEW_COMMAND_LOG=str(command_log),
    )
    if service_state is not None:
        environment["REVIEW_SERVICE_STATE"] = str(service_state)
    if failed_command is not None:
        environment["REVIEW_FAIL_COMMAND"] = failed_command
    assignments = "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())
    return subprocess.run(
        ["/bin/bash", "-u"], input=assignments + "\n" + recovery + "\n" + action + "\n",
        env=environment, capture_output=True, text=True,
    ), command_log


@pytest.mark.parametrize("rename_state, failed_command", [
    ("new_live_with_old_symlink_moved", None),
    ("old_link_removed_before_target_move", None),
    ("new_live_with_old_directory_moved", None),
    ("new_live_with_old_symlink_moved", "systemctl"),
    ("new_live_with_old_symlink_moved", "active-service"),
    ("new_live_with_old_symlink_moved", "mv"),
])
def test_controller_rollback_uses_durable_filesystem_state_without_dead_flags(tmp_path, rename_state, failed_command):
    install_root = tmp_path / "install"
    releases = install_root / "releases"
    releases.mkdir(parents=True)
    venv_root = install_root / "venvs"
    venv_root.mkdir()
    backup = releases / "server-before-fixture"
    backup_venv = releases / "venv-before-fixture"
    failed = releases / "server-failed-fixture"
    failed_venv = releases / "venv-failed-fixture"
    stage = install_root / ".deploy-fixture"
    state = releases / ".controller-prepare-fixture"
    old_target = venv_root / "controller-old-fixture"
    new_venv = venv_root / "controller-new-fixture"
    live = install_root / "server"
    live_venv = install_root / ".venv"
    _write_tree(backup, "old-server")
    _write_tree(new_venv, "new-venv")
    _write_tree(stage, "owned-stage")
    _write_tree(state, "owned-state")
    # A sibling control asset is outside all live/backup/stage paths.
    protected = install_root / "data" / "database-marker"
    protected.parent.mkdir()
    protected.write_text("persistent data")
    outside = tmp_path / "outside-marker"
    outside.write_text("outside installation")

    old_kind = "directory" if rename_state.endswith("directory_moved") else "symlink"
    if rename_state == "old_link_removed_before_target_move":
        _write_tree(old_target, "old-venv")
    else:
        _write_tree(live, "new-server")
        _write_tree(backup_venv, "old-venv")
        live_venv.symlink_to(new_venv)

    source = (ROOT / "deploy/deploy-controller-release.sh").read_text()
    start = source.index("rollback() {")
    end = source.index("\ntrap 'rollback $?'")
    recovery = source[start:end]
    values = {
        "install_root": str(install_root), "service": "fixture-controller",
        "backup": str(backup), "backup_venv": str(backup_venv),
        "failed": str(failed), "failed_venv": str(failed_venv),
        "old_venv_kind": old_kind, "old_venv_target": str(old_target),
        "new_venv": str(new_venv), "stage": str(stage), "state_dir": str(state),
        "live_change_started": "1",
    }
    # Removed assignment flags are intentionally never set. Rename state is
    # already durable, as it can be when a signal arrives between commands.
    result, command_log = _run_recovery(
        tmp_path, install_root, values, recovery, "rollback 143",
        failed_command=failed_command,
    )
    if failed_command:
        assert result.returncode == 70, result.stderr
        assert "requires operator attention" in result.stderr
        assert (state / "rollback-incomplete").is_file()
        assert (live / "identity").read_text() == "new-server"
        assert (backup / "identity").read_text() == "old-server"
        assert (backup_venv / "identity").read_text() == "old-venv"
        assert live_venv.resolve() == new_venv
        assert (new_venv / "identity").read_text() == "new-venv"
        assert (stage / "identity").read_text() == "owned-stage"
        assert protected.read_text() == "persistent data"
        assert outside.read_text() == "outside installation"
        return
    assert result.returncode == 143, result.stderr
    assert result.stderr == ""
    assert (live / "identity").read_text() == "old-server"
    expected_venv = old_target if old_kind == "symlink" else live_venv
    assert (expected_venv / "identity").read_text() == "old-venv"
    assert live_venv.is_symlink() is (old_kind == "symlink")
    if old_kind == "symlink":
        assert live_venv.resolve() == old_target
    assert not backup.exists()
    assert not backup_venv.exists()
    assert not new_venv.exists()
    assert not stage.exists()
    assert not state.exists()
    if rename_state != "old_link_removed_before_target_move":
        assert (failed / "identity").read_text() == "new-server"
        assert (failed_venv / "identity").read_text() == "new-venv"
    else:
        assert not failed.exists()
        assert not failed_venv.exists()
    assert protected.read_text() == "persistent data"
    assert outside.read_text() == "outside installation"
    operations = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert [entry["arguments"] for entry in operations if entry["command"] == "systemctl"] == [
        ["stop", "fixture-controller"], ["is-active", "--quiet", "fixture-controller"],
    ]
    for entry in operations:
        for argument in entry["arguments"]:
            if argument.startswith("/"):
                Path(argument).relative_to(install_root)


def _prepared_fixture(tmp_path):
    """Run the unchanged embedded Python program with fixture ownership only."""
    source = (ROOT / "deploy/deploy-controller-release.sh").read_text()
    start = source.index("prepared_state_action() {")
    start = source.index("<<'PY'\n", start) + len("<<'PY'\n")
    program = source[start:source.index("\nPY\n}", start)]
    definitions, dispatch = program.split("\naction = sys.argv[1]\n", 1)
    namespace = {}
    exec(compile(definitions, "prepared-state-fixture", "exec"), namespace)
    ownership = {}

    def owned(path, metadata):
        path = Path(path)
        path.relative_to(tmp_path)
        values = list(metadata)
        values[4] = ownership.get(path, 0)
        return os.stat_result(values)

    class FixturePath(type(Path())):
        def lstat(self):
            return owned(self, super().lstat())

    class FixtureOS:
        def __getattr__(self, name):
            return getattr(os, name)

        def scandir(self, path):
            with os.scandir(path) as entries:
                return [SimpleNamespace(
                    name=entry.name, path=entry.path,
                    stat=lambda *, follow_symlinks, entry=entry: owned(
                        entry.path, entry.stat(follow_symlinks=follow_symlinks)
                    ),
                ) for entry in entries]

    namespace.update(Path=FixturePath, os=FixtureOS())
    install_root = tmp_path / "install"
    stamp = "0.8.12-20261005T120000Z-123-" + "a" * 24
    stage = install_root / f".deploy-{stamp}"
    venv = install_root / "venvs" / f"controller-{stamp}"
    state = install_root / "releases" / f".controller-prepare-{stamp}"
    archive = tmp_path / "controller.tar.gz"
    archive.write_bytes(b"fixture archive identity")
    archive.chmod(0o600)
    for path, mode in ((stage, 0o700), (venv, 0o755), (state, 0o700),
                       (install_root / "server", 0o755), (install_root / ".venv", 0o755)):
        _write_tree(path, str(path.name))
        path.chmod(mode)
        (path / "identity").chmod(0o644)
    (venv / "lib").mkdir(mode=0o755)
    (venv / "lib64").symlink_to("lib")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    def run(action, *, ttl=7200):
        arguments = ["fixture", action, str(state), str(archive), digest,
                     "0.8.12", str(install_root), "fixture-controller"]
        if action == "create":
            arguments.extend((str(stage), str(venv), str(ttl)))
        namespace["sys"] = SimpleNamespace(argv=arguments)
        output = io.StringIO()
        with redirect_stdout(output):
            exec(compile("action = sys.argv[1]\n" + dispatch,
                         "prepared-state-fixture", "exec"), namespace)
        return output.getvalue().splitlines()

    assert run("create") == [str(state)]
    return SimpleNamespace(run=run, namespace=namespace, ownership=ownership,
                           install_root=install_root, stage=stage, venv=venv,
                           state=state, archive=archive)


@pytest.mark.parametrize("change, reason", [
    ("stage-content", "prepared server tree changed"),
    ("stage-inode", "prepared server tree changed"),
    ("stage-link", "path is not a direct directory"),
    ("stage-writable", "untrusted mode"),
    ("stage-owner", "untrusted ownership"),
    ("venv-link", "unexpected symbolic link"),
    ("live-content", "live Controller source changed"),
    ("archive-inode", "prepared archive identity changed"),
    ("token", "prepared token is invalid"),
    ("token-link", "path is not a direct regular file"),
    ("hmac", "prepared state HMAC is invalid"),
])
def test_prepared_state_rejects_changed_identity_before_activation(tmp_path, change, reason):
    fixture = _prepared_fixture(tmp_path)
    assert fixture.run("validate") == [str(fixture.stage), str(fixture.venv),
                                       "directory", str(fixture.install_root / ".venv")]
    if change == "stage-content":
        (fixture.stage / "identity").write_text("changed")
    elif change == "stage-inode":
        original = fixture.stage.with_name("original-stage")
        fixture.stage.rename(original)
        shutil.copytree(original, fixture.stage)
    elif change == "stage-link":
        original = fixture.stage.with_name("original-stage")
        fixture.stage.rename(original)
        fixture.stage.symlink_to(original)
    elif change == "stage-writable":
        (fixture.stage / "identity").chmod(0o666)
    elif change == "stage-owner":
        fixture.ownership[fixture.stage / "identity"] = 1000
    elif change == "venv-link":
        (fixture.venv / "lib64").unlink()
        (fixture.venv / "lib64").symlink_to(tmp_path)
    elif change == "live-content":
        (fixture.install_root / "server" / "identity").write_text("changed")
    elif change == "archive-inode":
        original = fixture.archive.with_name("original-archive")
        fixture.archive.rename(original)
        shutil.copyfile(original, fixture.archive)
        fixture.archive.chmod(0o600)
    elif change == "token":
        with (fixture.state / "token").open("a") as output:
            output.write("0")
    elif change == "token-link":
        token = fixture.state / "token"
        original = token.with_name("original-token")
        token.rename(original)
        token.symlink_to(original)
    else:
        path = fixture.state / "state.json"
        document = json.loads(path.read_text())
        document["payload"]["version"] = "0.0.0"
        path.write_text(json.dumps(document))
    with pytest.raises(SystemExit, match=reason):
        fixture.run("validate")
    assert fixture.stage.exists()
    assert fixture.state.exists()


def test_stale_prepared_state_can_be_aborted_but_not_activated(tmp_path):
    fixture = _prepared_fixture(tmp_path)
    payload = json.loads((fixture.state / "state.json").read_text())["payload"]
    fixture.namespace["time"] = SimpleNamespace(time=lambda: payload["expires_at"] + 1)
    with pytest.raises(SystemExit, match="prepared state is stale"):
        fixture.run("validate")
    assert fixture.run("abort") == [str(fixture.stage), str(fixture.venv)]


@pytest.mark.parametrize("action", ["validate", "abort"])
def test_incomplete_rollback_preserves_signed_state_and_requires_review(tmp_path, action):
    fixture = _prepared_fixture(tmp_path)
    signed_state = {name: (fixture.state / name).read_bytes() for name in ("token", "state.json")}
    (fixture.state / "rollback-incomplete").write_text("runtime recovery failed\n")
    with pytest.raises(SystemExit, match="runtime rollback is incomplete"):
        fixture.run(action)
    assert {name: (fixture.state / name).read_bytes() for name in signed_state} == signed_state
    assert fixture.stage.is_dir() and fixture.venv.is_dir()


def test_commit_receipt_binds_the_active_live_pair(tmp_path):
    fixture = _prepared_fixture(tmp_path)
    shutil.rmtree(fixture.install_root / ".venv")
    (fixture.install_root / ".venv").symlink_to(fixture.venv)
    assert fixture.run("mark-committed") == [str(fixture.state)]
    assert fixture.run("finalize") == [str(fixture.state)]
    (fixture.install_root / "server" / "identity").write_text("later live change")
    with pytest.raises(SystemExit, match="committed live identity changed"):
        fixture.run("finalize")


@pytest.mark.parametrize("schema", [True, 1.0, 1])
def test_deploy_manifest_requires_a_json_integer_schema(tmp_path, schema):
    archive_path = tmp_path / "controller.tar.gz"
    manifest = json.dumps({"schema_version": schema, "version": "0.8.12", "files": {}}).encode()
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("controller-release.json")
        member.size = len(manifest)
        archive.addfile(member, io.BytesIO(manifest))
    source = (ROOT / "deploy/deploy-controller-release.sh").read_text()
    start = source.index("# Validate the manifest and every archive member")
    start = source.index("<<'PY'\n", start) + len("<<'PY'\n")
    program = source[start:source.index("\nPY\n", start)]
    result = subprocess.run(
        [sys.executable, "-", str(archive_path), str(tmp_path / "stage"), "0.8.12", str(tmp_path)],
        input=program, capture_output=True, text=True,
    )
    reason = "controller release file allowlist does not match" if type(schema) is int else "controller release manifest does not match this host"
    assert result.returncode != 0
    assert reason in result.stderr


