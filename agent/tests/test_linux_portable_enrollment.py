"""Run the portable entry with local FakeAgent, helpers and bounded PTY input.

Only the Linux/x64/root guard is removed from the temporary script copy. The
actual package, config, enrollment and final exec paths run without installation,
HTTP, mining, systemd, VM access or a real Agent. All files belong to tmp_path.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import time

import pytest


ENTRY = Path(__file__).resolve().parents[2] / "deploy/linux/portable/Start-Portable.sh"
NEW_CODE = "synthetic-portable-token-123456"
INHERIT_CODE = "rpr_" + "A" * 43
TOKEN_VARS = ("RIGPILOT_ENROLLMENT_TOKEN", "RIGPILOT_REPLACEMENT_TOKEN")
PRIVATE_VARS = ("PORTABLE_ENROLLMENT_TOKEN", "PORTABLE_REPLACEMENT_TOKEN")
pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="portable shell harness requires POSIX and Bash",
)


def _pty_run(args, env, answers):
    import pty
    import termios

    master, slave = pty.openpty()
    process = None
    output = bytearray()
    deadline = time.monotonic() + 8
    try:
        process = subprocess.Popen(
            args, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
            env=env, start_new_session=True,
        )
        os.close(slave)
        slave = None
        for prompt, answer, secret in answers:
            while prompt.encode() not in output:
                remaining = deadline - time.monotonic()
                assert remaining > 0, "portable entry did not reach expected prompt"
                ready, _, _ = select.select([master], [], [], remaining)
                assert ready, "portable entry did not reach expected prompt"
                output.extend(os.read(master, 4096))
            if secret:
                # The printf can precede read -s changing the PTY echo mode.
                while termios.tcgetattr(master)[3] & termios.ECHO:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and process.poll() is None
                    select.select([], [], [], min(0.01, remaining))
            payload = (answer + "\n").encode()
            assert os.write(master, payload) == len(payload)
        _, stderr = process.communicate(timeout=max(0.1, deadline - time.monotonic()))
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return subprocess.CompletedProcess(args, process.returncode, output.decode(), stderr.decode())
    finally:
        if process is not None:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=2)
            if process.stderr is not None:
                process.stderr.close()
        if slave is not None:
            os.close(slave)
        os.close(master)


@pytest.fixture
def portable(tmp_path):
    source = ENTRY.read_text()
    guards = (
        '[[ "$(uname -s)" == "Linux" ]] || fail "Linux is required"\n'
        'case "$(uname -m)" in x86_64|amd64) ;; *) fail "x86_64 is required" ;; esac\n'
        '[[ "${EUID}" -eq 0 ]] || fail "run with sudo for mining, GPU and power actions"\n'
    )
    assert source.count(guards) == 1
    entry = tmp_path / "Start-Portable.sh"
    entry.write_text(source.replace(guards, "# Local fixture bypasses only OS/architecture/root guards.\n"))
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.json"
    state = data / "state.json"
    calls = tmp_path / "fake-calls.jsonl"
    helpers = tmp_path / "helper-env.jsonl"
    fake = tmp_path / "RigPilotAgent"
    fake.write_text("#!" + sys.executable + "\n" + '''import hashlib, json, os, pathlib, sys
assert sys.argv[1:3] == ['--config', str(pathlib.Path(__file__).parent / 'data/config.json')]
once = sys.argv[3:] == ['--once']
assert once or sys.argv[3:] == []
config = json.loads(pathlib.Path(sys.argv[2]).read_text())
state = pathlib.Path(config['state_file'])
enrollment = state.with_name('.' + state.name + '.enrollment-pending')
replacement = state.with_name('.' + state.name + '.replacement-pending')
new = os.environ.get('RIGPILOT_ENROLLMENT_TOKEN')
inherit = os.environ.get('RIGPILOT_REPLACEMENT_TOKEN')
assert not (new is not None and inherit is not None)
assert not any(k in os.environ for k in ('PORTABLE_ENROLLMENT_TOKEN', 'PORTABLE_REPLACEMENT_TOKEN'))
receipt = {'once': once, 'args': sys.argv[1:], 'server_url': config['server_url'], 'name': config.get('name'),
           'enrollment_env_present': new is not None, 'replacement_env_present': inherit is not None,
           'original_new': new == 'synthetic-portable-token-123456',
           'original_inherit': inherit == 'rpr_' + 'A' * 43}
with open(os.environ['PORTABLE_TEST_CALLS'], 'a') as stream:
    stream.write(json.dumps(receipt) + '\\n')
if not once:
    assert new is None and inherit is None and state.exists()
    sys.exit(0)
mode = os.environ['PORTABLE_TEST_MODE']
if mode == 'zero_without_state':
    sys.exit(0)
if mode == 'confirmed_rejected':
    enrollment.unlink(missing_ok=True)
    sys.exit(10)
if mode == 'rejected_but_pending':
    sys.exit(10)
journal = replacement if inherit is not None or replacement.exists() else enrollment
code = inherit if journal == replacement else new
if os.path.lexists(journal):
    if journal.is_symlink() or not journal.is_file():
        sys.exit(2)
    try:
        saved = json.loads(journal.read_text())
    except ValueError:
        sys.exit(2)
    assert saved['server_url'] == config['server_url'] and saved['name'] == config.get('name')
    if code is None:
        if journal == replacement or mode != 'recover_without_code':
            sys.exit(2)
    elif hashlib.sha256(code.encode()).hexdigest() != saved['code_sha256']:
        sys.exit(2)
else:
    assert code is not None
    journal.write_text(json.dumps({'server_url': config['server_url'], 'name': config.get('name'),
                                  'code_sha256': hashlib.sha256(code.encode()).hexdigest()}))
if mode == 'uncertain':
    sys.exit(2)
state.write_text('{"agent_id":"synthetic-portable-agent","agent_secret":"synthetic-secret"}')
journal.unlink()
sys.exit(0)
''')
    fake.chmod(0o700)
    bin_dir = tmp_path / "helpers"
    bin_dir.mkdir()
    # Observe helper inheritance without replacing config logic or filesystem
    # behavior; delegate only these safe local tools after checking variables.
    for tool in ("dirname", "install", "python3"):
        actual = sys.executable if tool == "python3" else shutil.which(tool)
        assert actual is not None
        wrapper = bin_dir / tool
        wrapper.write_text("#!" + sys.executable + "\n" +
            "import json, os, sys\n" +
            f"names = {TOKEN_VARS + PRIVATE_VARS!r}\n" +
            "present = [k for k in names if k in os.environ]\n" +
            "with open(os.environ['PORTABLE_TEST_HELPERS'], 'a') as stream:\n" +
            f"    stream.write(json.dumps({{'tool': {tool!r}, 'present': present}}) + '\\n')\n" +
            "assert not present\n" +
            f"os.execv({actual!r}, [{actual!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o700)
    environment = {
        "PATH": str(bin_dir) + os.pathsep + os.defpath,
        "LC_ALL": "C", "PORTABLE_TEST_CALLS": str(calls),
        "PORTABLE_TEST_HELPERS": str(helpers), "PORTABLE_TEST_MODE": "success",
    }

    class Harness:
        def config(self):
            config.write_text(json.dumps({
                "server_url": "https://synthetic-controller.invalid", "name": "original-device",
                "state_file": str(state), "custom_metadata": {"preserve": True},
                "allow_remote_sessions": True, "allow_remote_root_sessions": False,
            }))

        def journal(self, kind, code=None):
            target = data / f".state.json.{kind}-pending"
            value = json.loads(config.read_text())
            code = code or (NEW_CODE if kind == "enrollment" else INHERIT_CODE)
            target.write_text(json.dumps({"server_url": value["server_url"], "name": value["name"],
                "code_sha256": hashlib.sha256(code.encode()).hexdigest()}))
            return target

        def run(self, values=None, *, mode="success", answers=None):
            env = {**environment, "PORTABLE_TEST_MODE": mode, **(values or {})}
            args = [shutil.which("bash"), str(entry)]
            if answers is not None:
                result = _pty_run(args, env, answers)
            else:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    env=env, start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=8)
                    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=2)
            assert NEW_CODE not in result.stdout + result.stderr
            assert INHERIT_CODE not in result.stdout + result.stderr
            if config.exists():
                assert NEW_CODE not in config.read_text() and INHERIT_CODE not in config.read_text()
            assert all(not row["present"] for row in self.helpers())
            return result

        def calls(self):
            return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

        def helpers(self):
            return [json.loads(line) for line in helpers.read_text().splitlines()] if helpers.exists() else []

    harness = Harness()
    harness.config_path, harness.state = config, state
    return harness


def _metadata(code_var, code):
    return {code_var: code, "RIGPILOT_CONTROLLER_URL": "https://explicit-controller.invalid",
            "RIGPILOT_DEVICE_NAME": "visible-device"}


@pytest.mark.parametrize("code_var,code,inherit", [
    (TOKEN_VARS[0], NEW_CODE, False), (TOKEN_VARS[1], INHERIT_CODE, True),
])
def test_fresh_env_mode_reaches_selected_once_then_secret_free_final_exec(portable, code_var, code, inherit):
    result = portable.run({**_metadata(code_var, code), PRIVATE_VARS[0]: "ambient-private"})
    assert result.returncode == 0, result.stderr
    once, final = portable.calls()
    assert once["once"] and not final["once"]
    assert once["replacement_env_present"] is inherit
    assert once["enrollment_env_present"] is (not inherit)
    assert not final["replacement_env_present"] and not final["enrollment_env_present"]
    assert once["server_url"] == "https://explicit-controller.invalid" and once["name"] == "visible-device"
    assert all(code not in json.dumps(call) for call in portable.calls())
    settings = json.loads(portable.config_path.read_text())
    assert "allow_remote_sessions" not in settings and "allow_remote_root_sessions" not in settings
    assert "allow_nvidia_driver_updates" not in settings
    assert not settings["allow_agent_updates"]
    assert portable.config_path.stat().st_mode & 0o777 == 0o600
    assert {row["tool"] for row in portable.helpers()} == {"dirname", "install", "python3"}
    assert "Registration type:" not in result.stdout


@pytest.mark.parametrize("choice,code,inherit", [("", NEW_CODE, False), ("1", NEW_CODE, False), ("2", INHERIT_CODE, True)])
def test_fresh_real_pty_choice_and_hidden_input(portable, choice, code, inherit):
    result = portable.run(answers=[
        ("Controller URL [", "https://interactive-controller.invalid", False),
        ("Device remark/name (optional): ", "interactive-device", False),
        ("Registration type: ", choice, False),
        ("Device inheritance code: " if inherit else "One-time enrollment code: ", code, True),
    ])
    assert result.returncode == 0, result.stderr
    assert portable.calls()[0]["replacement_env_present"] is inherit
    assert portable.calls()[0]["enrollment_env_present"] is (not inherit)


@pytest.mark.parametrize("overrides", [
    {"RIGPILOT_CONTROLLER_URL": ""}, {"RIGPILOT_DEVICE_NAME": ""},
    {"RIGPILOT_DEVICE_NAME": "   "}, {"RIGPILOT_DEVICE_NAME": "x" * 81},
    {"RIGPILOT_CONTROLLER_URL": "http://unsafe.invalid"},
    {"RIGPILOT_CONTROLLER_URL": "https://user:password@controller.invalid"},
    {"RIGPILOT_CONTROLLER_URL": "https://controller.invalid?query=1"},
    {"RIGPILOT_CONTROLLER_URL": "https://controller.invalid:0"},
    {"RIGPILOT_CONTROLLER_URL": "https://controller.invalid:not-a-port"},
    {"RIGPILOT_CONTROLLER_URL": "https://controller.invalid:65536"},
    {"RIGPILOT_CONTROLLER_URL": "https://controller.invalid\\other"},
])
def test_unattended_fresh_config_requires_safe_explicit_metadata(portable, overrides):
    result = portable.run({**_metadata(TOKEN_VARS[1], INHERIT_CODE), **overrides})
    assert result.returncode != 0 and not portable.config_path.exists() and not portable.calls()
    assert "Controller URL [" not in result.stdout


def test_no_code_or_dual_codes_refuse_fresh_configuration(portable):
    assert portable.run().returncode == 1
    dual = portable.run({**_metadata(TOKEN_VARS[0], NEW_CODE), TOKEN_VARS[1]: INHERIT_CODE})
    assert dual.returncode == 1 and "cannot be supplied together" in dual.stderr
    assert not portable.config_path.exists() and not portable.calls()


def test_dual_codes_refuse_before_changing_existing_unclaimed_metadata(portable):
    portable.config()
    original = portable.config_path.read_bytes()
    result = portable.run({**_metadata(TOKEN_VARS[0], NEW_CODE), TOKEN_VARS[1]: INHERIT_CODE})
    assert result.returncode == 1 and "cannot be supplied together" in result.stderr
    assert portable.config_path.read_bytes() == original and not portable.calls()


def test_saved_identity_reuses_exact_config_and_clears_ambient_codes(portable):
    portable.config()
    original_config = portable.config_path.read_bytes()
    portable.state.write_text('{"agent_id":"existing-device"}')
    original_state = portable.state.read_bytes()
    result = portable.run({**_metadata(TOKEN_VARS[0], NEW_CODE), TOKEN_VARS[1]: INHERIT_CODE})
    assert result.returncode == 0, result.stderr
    assert portable.config_path.read_bytes() == original_config and portable.state.read_bytes() == original_state
    assert len(portable.calls()) == 1 and not portable.calls()[0]["once"]
    assert not portable.calls()[0]["enrollment_env_present"] and not portable.calls()[0]["replacement_env_present"]
    assert "Registration type:" not in result.stdout


@pytest.mark.parametrize("saved", ["identity", "enrollment", "replacement"])
def test_saved_identity_or_pending_without_config_cannot_create_new_config(portable, saved):
    if saved == "identity":
        portable.state.write_text('{"agent_id":"existing-device"}')
        protected = portable.state
    else:
        portable.config()
        protected = portable.journal(saved)
        portable.config_path.unlink()
    original = protected.read_bytes()
    result = portable.run(_metadata(TOKEN_VARS[0], NEW_CODE))
    assert result.returncode == 1 and "no Agent configuration" in result.stderr
    assert not portable.config_path.exists() and not portable.calls() and protected.read_bytes() == original


@pytest.mark.parametrize("kind,code_var,code", [
    ("enrollment", TOKEN_VARS[0], NEW_CODE), ("replacement", TOKEN_VARS[1], INHERIT_CODE),
])
def test_pending_kind_metadata_and_original_code_are_preserved(portable, kind, code_var, code):
    portable.config()
    journal = portable.journal(kind)
    original_config, original_journal = portable.config_path.read_bytes(), journal.read_bytes()
    other_var = TOKEN_VARS[1] if kind == "enrollment" else TOKEN_VARS[0]
    other_code = INHERIT_CODE if kind == "enrollment" else NEW_CODE
    wrong_kind = portable.run({other_var: other_code})
    assert wrong_kind.returncode == 1 and not portable.calls()
    wrong_binding = portable.run({**_metadata(code_var, code)})
    assert wrong_binding.returncode == 1 and not portable.calls()
    assert "bound to its existing Controller URL and device name" in wrong_binding.stderr
    wrong_code = "synthetic-wrong-original-123456" if kind == "enrollment" else "rpr_" + "B" * 43
    rejected = portable.run({code_var: wrong_code})
    assert rejected.returncode == 1 and not portable.state.exists()
    assert portable.config_path.read_bytes() == original_config and journal.read_bytes() == original_journal
    success = portable.run({code_var: code})
    assert success.returncode == 0, success.stderr
    assert portable.state.exists() and not journal.exists()
    assert portable.config_path.read_bytes() == original_config


def test_dual_pending_journals_refuse_without_call_or_mutation(portable):
    portable.config()
    journals = [portable.journal(kind) for kind in ("enrollment", "replacement")]
    original = [path.read_bytes() for path in journals]
    result = portable.run({TOKEN_VARS[0]: NEW_CODE})
    assert result.returncode == 1 and "both registration and inheritance are pending" in result.stderr
    assert not portable.calls() and [path.read_bytes() for path in journals] == original


@pytest.mark.parametrize("kind,code_var,code", [
    ("enrollment", TOKEN_VARS[0], NEW_CODE), ("replacement", TOKEN_VARS[1], INHERIT_CODE),
])
def test_uncertain_result_retains_pending_for_same_attempt_retry(portable, kind, code_var, code):
    first = portable.run(_metadata(code_var, code), mode="uncertain")
    journal = portable.config_path.parent / f".state.json.{kind}-pending"
    assert first.returncode == 1 and not portable.state.exists() and journal.exists()
    original_config, original_journal = portable.config_path.read_bytes(), journal.read_bytes()
    again = portable.run({code_var: code}, mode="uncertain")
    assert again.returncode == 1 and journal.read_bytes() == original_journal
    retry = portable.run({code_var: code})
    assert retry.returncode == 0, retry.stderr
    assert portable.state.exists() and not journal.exists() and portable.config_path.read_bytes() == original_config


def test_pending_enrollment_recovers_without_code_then_execs_without_secret(portable):
    portable.config()
    journal = portable.journal("enrollment")
    result = portable.run(mode="recover_without_code")
    assert result.returncode == 0, result.stderr
    assert portable.state.exists() and not journal.exists()
    assert [call["once"] for call in portable.calls()] == [True, False]
    assert all(not call["enrollment_env_present"] and not call["replacement_env_present"] for call in portable.calls())


@pytest.mark.parametrize("kind,code", [("enrollment", NEW_CODE), ("replacement", INHERIT_CODE)])
def test_pending_real_pty_prompts_for_original_code_only(portable, kind, code):
    portable.config()
    portable.journal(kind)
    prompt = "Original enrollment code (must be the same code; leave blank to exit): " if kind == "enrollment" else "Original inheritance code (must be the same code): "
    result = portable.run(answers=[(prompt, code, True)])
    assert result.returncode == 0, result.stderr
    assert "Registration type:" not in result.stdout and "One-time enrollment code:" not in result.stdout
    assert len(portable.calls()) == (3 if kind == "enrollment" else 2)


def test_pending_inheritance_without_original_code_refuses_noninteractive(portable):
    portable.config()
    journal = portable.journal("replacement")
    original = journal.read_bytes()
    result = portable.run()
    assert result.returncode == 1 and "original inheritance code" in result.stderr
    assert not portable.calls() and journal.read_bytes() == original


@pytest.mark.parametrize("mode,message", [
    ("zero_without_state", "identity was not saved"),
    ("confirmed_rejected", "Controller confirmed registration was not created"),
    ("rejected_but_pending", "retry Start-Portable.sh with the original enrollment code"),
])
def test_agent_exit_without_confirmed_identity_does_not_enter_final_exec(portable, mode, message):
    portable.config()
    journal = portable.journal("enrollment")
    original = journal.read_bytes()
    result = portable.run({TOKEN_VARS[0]: NEW_CODE}, mode=mode)
    assert result.returncode == 1 and message in result.stderr and not portable.state.exists()
    assert len(portable.calls()) == 1 and portable.calls()[0]["once"]
    if mode != "confirmed_rejected":
        assert journal.read_bytes() == original


def test_unclaimed_existing_config_changes_only_explicit_metadata(portable):
    portable.config()
    original = json.loads(portable.config_path.read_text())
    result = portable.run(_metadata(TOKEN_VARS[1], INHERIT_CODE))
    assert result.returncode == 0, result.stderr
    value = json.loads(portable.config_path.read_text())
    assert value == {**original, "server_url": "https://explicit-controller.invalid", "name": "visible-device"}


@pytest.mark.parametrize("code_var,code", [
    (TOKEN_VARS[0], ""), (TOKEN_VARS[0], "short"), (TOKEN_VARS[0], "x" * 257),
    (TOKEN_VARS[1], ""), (TOKEN_VARS[1], "rpr_bad"),
])
def test_invalid_selected_code_refuses_before_agent_call(portable, code_var, code):
    portable.config()
    original = portable.config_path.read_bytes()
    result = portable.run({code_var: code})
    assert result.returncode == 1 and not portable.calls() and not portable.state.exists()
    assert portable.config_path.read_bytes() == original


@pytest.mark.parametrize("shape", ["corrupt", "dangling_link", "linked_file", "directory"])
def test_invalid_private_journal_is_left_for_agent_validation(portable, shape):
    portable.config()
    journal = portable.config_path.parent / ".state.json.enrollment-pending"
    target = journal.with_name("synthetic-private-target")
    if shape == "dangling_link":
        journal.symlink_to(target)
    elif shape == "linked_file":
        target.write_text("synthetic-private-target-data")
        journal.symlink_to(target)
    elif shape == "directory":
        journal.mkdir()
    else:
        journal.write_text("synthetic-malformed-private-journal")
    original = journal.lstat()
    result = portable.run({TOKEN_VARS[0]: NEW_CODE})
    assert result.returncode == 1 and len(portable.calls()) == 1 and not portable.state.exists()
    assert journal.lstat().st_ino == original.st_ino
    if shape == "linked_file":
        assert target.read_text() == "synthetic-private-target-data"
    if shape == "corrupt":
        assert journal.read_text() == "synthetic-malformed-private-journal"
    assert "synthetic-malformed-private-journal" not in result.stdout + result.stderr
