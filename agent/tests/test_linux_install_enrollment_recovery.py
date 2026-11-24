"""Execute only Install.sh's enrollment function using a synthetic local Agent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).resolve().parents[2] / "deploy/linux/portable/Install.sh"
pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None, reason="installer registration requires a POSIX shell")


def _function(source, name):
    match = re.search(r"(?ms)^" + re.escape(name) + r"\(\) \{\n.*?^\}", source)
    assert match is not None, name
    return match.group(0)


def _secret_bootstrap(source):
    start = source.index("# Capture explicit one-time codes before any package/configuration helper")
    end = source.index("\nreadonly CONTROLLER_DEFAULT", start)
    return source[start:end]


def test_fresh_install_without_config_continues_past_preclaim_helper(tmp_path):
    source = INSTALLER.read_text(encoding="utf-8")
    state = tmp_path / "state.json"
    config = tmp_path / "config.json"
    harness = (
        "set -Eeuo pipefail\n"
        + _function(source, "fail") + "\n"
        + _function(source, "prepare_existing_identity_config") + "\n"
        + f'STATE_FILE="{state}" CONFIG_FILE="{config}"\n'
        + "prepare_existing_identity_config\necho continued\n"
    )
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "continued\n"
    assert not config.exists()


@pytest.fixture
def installer_registration(tmp_path):
    source = INSTALLER.read_text()
    entry = 'if [[ ! -s "${STATE_FILE}" ]]; then\n    enroll_agent_identity\nfi\nunset INSTALL_ENROLLMENT_TOKEN INSTALL_REPLACEMENT_TOKEN'
    assert entry in source
    harness = tmp_path / "enrollment-only.sh"
    probe = """python3 - <<'PYPROBE'
import json, os
from pathlib import Path
names = ('RIGPILOT_ENROLLMENT_TOKEN', 'RIGPILOT_REPLACEMENT_TOKEN',
         'INSTALL_ENROLLMENT_TOKEN', 'INSTALL_REPLACEMENT_TOKEN')
Path(os.environ['RIGPILOT_TEST_PROBE']).write_text(json.dumps([key for key in names if key in os.environ]))
PYPROBE
"""
    harness.write_text(
        "set -Eeuo pipefail\n" + _secret_bootstrap(source) + "\n"
        + _function(source, "fail") + "\n"
        + 'RIGPILOT_TEST_PROBE="${RIGPILOT_TEST_PRE_HELPER}" ' + probe
        + _function(source, "prepare_existing_identity_config") + "\n"
        + _function(source, "enroll_agent_identity") + "\n"
        + "prepare_existing_identity_config\n" + entry + "\n"
        + 'RIGPILOT_TEST_PROBE="${RIGPILOT_TEST_POST_HELPER}" ' + probe
    )
    state = tmp_path / "state.json"
    pending = tmp_path / ".state.json.enrollment-pending"
    replacement_pending = tmp_path / ".state.json.replacement-pending"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_file": str(state), "server_url": "https://synthetic-controller.invalid"}))
    receipt = tmp_path / "fake-call.json"
    fake = tmp_path / "RigPilotAgent"
    fake.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
assert sys.argv[1:] == ['--config', os.environ['CONFIG_FILE'], '--once']
config = json.loads(pathlib.Path(sys.argv[2]).read_text())
state = pathlib.Path(config['state_file'])
pending = state.with_name('.' + state.name + '.enrollment-pending')
replacement_pending = state.with_name('.' + state.name + '.replacement-pending')
receipt = {'args': sys.argv[1:], 'server_url': config['server_url'], 'name': config.get('name'),
           'enrollment_env_present': 'RIGPILOT_ENROLLMENT_TOKEN' in os.environ,
           'replacement_env_present': 'RIGPILOT_REPLACEMENT_TOKEN' in os.environ,
           'token_matches_typed': os.environ.get('RIGPILOT_ENROLLMENT_TOKEN') == 'synthetic-typed-token-123456',
           'replacement_matches_typed': os.environ.get('RIGPILOT_REPLACEMENT_TOKEN') == 'rpr_' + 'A' * 43}
receipt_path = pathlib.Path(os.environ['RIGPILOT_TEST_RECEIPT'])
receipt['call_number'] = json.loads(receipt_path.read_text())['call_number'] + 1 if receipt_path.exists() else 1
receipt_path.write_text(json.dumps(receipt))
mode = os.environ['RIGPILOT_TEST_MODE']
if mode == 'success' or (mode == 'retry_original' and receipt['token_matches_typed']) or (mode == 'retry_replacement' and receipt['replacement_matches_typed']):
    state.write_text(json.dumps({'agent_id': 'synthetic-agent', 'agent_secret': 'synthetic-secret'}))
    pending.unlink(missing_ok=True)
    replacement_pending.unlink(missing_ok=True)
    sys.exit(0)
if mode == 'new_fail_pending':
    pending.write_text('synthetic-original-pending')
    sys.exit(2)
if mode == 'inherit_fail_pending':
    replacement_pending.write_text('synthetic-original-replacement-pending')
    sys.exit(2)
if mode == 'confirmed_rejected':
    pending.unlink(missing_ok=True)
    sys.exit(10)
if mode == 'rejected_but_pending':
    sys.exit(10)
if mode == 'zero_without_state':
    sys.exit(0)
sys.exit(2)
''')
    fake.chmod(0o700)
    environment = {key: value for key, value in os.environ.items()
                   if key not in {'RIGPILOT_ENROLLMENT_TOKEN', 'RIGPILOT_REPLACEMENT_TOKEN',
                                  'RIGPILOT_CONTROLLER_URL', 'RIGPILOT_DEVICE_NAME'}}
    environment.update(
        SOURCE_ROOT=str(tmp_path), CONFIG_FILE=str(config), STATE_FILE=str(state),
        RIGPILOT_TEST_RECEIPT=str(receipt), RIGPILOT_TEST_MODE="success",
        RIGPILOT_TEST_PRE_HELPER=str(tmp_path / "pre-helper-env.json"),
        RIGPILOT_TEST_POST_HELPER=str(tmp_path / "post-helper-env.json"),
    )

    def run(mode="success", interactive=False, token="synthetic-typed-token-123456", *, choice="", env_tokens=None):
        env = {**environment, "RIGPILOT_TEST_MODE": mode, **(env_tokens or {})}
        if not interactive:
            return subprocess.run(["bash", str(harness)], input="must-not-read-a-new-token\n", capture_output=True, text=True, env=env, timeout=5)
        import pty
        import select
        import termios
        import time
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(["bash", str(harness)], stdin=slave, stdout=slave, stderr=subprocess.PIPE, text=True, env=env)
            os.close(slave)
            slave = None
            deadline = time.monotonic() + 5
            prefix = bytearray()
            menu = b"Registration type: [1] New device (default), [2] Inherit existing device: "
            prompts = (
                b"One-time enrollment code: ",
                b"Device inheritance code: ",
                b"Original enrollment code (must be the same code; leave blank to exit): ",
                b"Original inheritance code (must be the same code): ",
            )
            sent_choice = False
            # Exercise the actual prompt/read path. Sending before read -s
            # changes terminal mode can lose queued input to a termios flush.
            while not any(prompt in prefix for prompt in prompts):
                remaining = deadline - time.monotonic()
                assert remaining > 0, "installer did not reach an enrollment prompt"
                ready, _, _ = select.select([master], [], [], remaining)
                assert ready, "installer did not reach an enrollment prompt"
                chunk = os.read(master, 4096)
                assert chunk, "installer exited before an enrollment prompt"
                prefix.extend(chunk)
                if menu in prefix and not sent_choice:
                    selection = (choice + "\n").encode()
                    assert os.write(master, selection) == len(selection)
                    sent_choice = True
            # printf precedes read -s, so the prompt alone is not sufficient:
            # wait until Bash has actually disabled input echo for that read.
            while termios.tcgetattr(master)[3] & termios.ECHO:
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"installer did not enter its silent token read; output={prefix!r}; process={process.poll()}"
                assert process.poll() is None, "installer exited before its token read"
                select.select([], [], [], min(0.01, remaining))
            payload = (token + "\n").encode()
            assert os.write(master, payload) == len(payload)
            _, stderr = process.communicate(timeout=5)
            output = bytearray(prefix)
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            return subprocess.CompletedProcess(process.args, process.returncode, output.decode(), stderr)
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=5)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            if slave is not None:
                os.close(slave)
            os.close(master)

    return state, pending, receipt, config, run


def test_existing_pending_recovers_noninteractively_without_any_token_environment(installer_registration):
    state, pending, receipt, config, run = installer_registration
    pending.write_text("synthetic-private-pending-do-not-display")
    result = run()
    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text())["agent_id"] == "synthetic-agent"
    assert not pending.exists()
    call = json.loads(receipt.read_text())
    assert call["args"] == ["--config", str(config), "--once"]
    assert call["enrollment_env_present"] is call["replacement_env_present"] is False
    assert "One-time enrollment code:" not in result.stdout
    assert "synthetic-private-pending-do-not-display" not in result.stdout + result.stderr


@pytest.mark.parametrize("shape", ["regular", "corrupt", "dangling_link", "linked_file", "directory"])
def test_uncertain_failure_keeps_pending_and_defers_validation_to_agent(installer_registration, shape):
    state, pending, receipt, _config, run = installer_registration
    target = pending.parent / "synthetic-linked-target"
    if shape == "dangling_link":
        pending.symlink_to(target)
    elif shape == "linked_file":
        target.write_text("synthetic-private-target")
        pending.symlink_to(target)
    elif shape == "directory":
        pending.mkdir()
    else:
        pending.write_text("{" if shape == "corrupt" else "synthetic-private-pending")
    before = pending.lstat()
    result = run("uncertain")
    assert result.returncode == 1
    assert receipt.exists(), "the installer must let the Agent validate every pending shape"
    assert pending.lstat().st_ino == before.st_ino
    assert not state.exists()
    assert "preserve this Agent directory" in result.stderr
    assert "valid new one-time code" not in result.stderr
    assert "One-time enrollment code:" not in result.stdout
    assert "synthetic-private" not in result.stdout + result.stderr
    call = json.loads(receipt.read_text())
    assert call["enrollment_env_present"] is call["replacement_env_present"] is False


def test_confirmed_rejection_after_cleanup_allows_a_new_code_on_next_run(installer_registration):
    state, pending, receipt, _config, run = installer_registration
    pending.write_text("synthetic-pending")
    result = run("confirmed_rejected", env_tokens={"RIGPILOT_ENROLLMENT_TOKEN": "synthetic-typed-token-123456"})
    assert result.returncode == 1
    assert not pending.exists() and not state.exists()
    assert "Controller confirmed registration was not created" in result.stderr
    assert "valid new one-time code" in result.stderr
    receipt.unlink()
    retry = run(interactive=True)
    assert retry.returncode == 0, retry.stderr
    assert state.exists()
    call = json.loads(receipt.read_text())
    assert call["enrollment_env_present"] is True
    assert call["replacement_env_present"] is False
    assert call["token_matches_typed"] is True


def test_rejection_code_does_not_advise_new_token_while_pending_remains(installer_registration):
    state, pending, receipt, _config, run = installer_registration
    pending.write_text("synthetic-pending")
    result = run("rejected_but_pending", env_tokens={"RIGPILOT_ENROLLMENT_TOKEN": "synthetic-typed-token-123456"})
    assert result.returncode == 1 and pending.exists() and not state.exists()
    assert receipt.exists()
    assert "preserve this Agent directory" in result.stderr
    assert "valid new one-time code" not in result.stderr


def test_no_pending_still_requires_interactive_first_registration(installer_registration):
    state, pending, receipt, _config, run = installer_registration
    result = run()
    assert result.returncode == 1
    assert "interactive choice or exactly one enrollment/inheritance code" in result.stderr
    assert not state.exists() and not pending.exists() and not receipt.exists()


@pytest.mark.parametrize("ambient", [
    {},
    {"RIGPILOT_REPLACEMENT_TOKEN": "rpr_" + "A" * 43},
    {"RIGPILOT_ENROLLMENT_TOKEN": "synthetic-typed-token-123456",
     "RIGPILOT_REPLACEMENT_TOKEN": "rpr_" + "A" * 43},
    {"RIGPILOT_REPLACEMENT_TOKEN": "rpr_" + "A" * 43,
     "RIGPILOT_CONTROLLER_URL": "https://different-controller.invalid",
     "RIGPILOT_DEVICE_NAME": "different-name"},
])
def test_existing_identity_does_not_reenroll_or_prompt(installer_registration, ambient):
    state, pending, receipt, config, run = installer_registration
    original = '{"agent_id":"synthetic-existing"}'
    original_config = config.read_text()
    state.write_text(original)
    pending.write_text("synthetic-stale-pending")
    # The image firstboot writes config+state before invoking Full Install.sh.
    # Even inherited ambient codes cannot start a second identity claim.
    result = run(env_tokens=ambient)
    assert result.returncode == 0
    assert state.read_text() == original and pending.exists() and not receipt.exists()
    assert config.read_text() == original_config
    assert result.stdout == "" and result.stderr == ""
    assert json.loads((config.parent / "pre-helper-env.json").read_text()) == []
    assert json.loads((config.parent / "post-helper-env.json").read_text()) == []


def test_no_pending_retry_corrects_controller_before_inheritance_claim(installer_registration):
    state, pending, receipt, config, run = installer_registration
    first = run(env_tokens={
        "RIGPILOT_REPLACEMENT_TOKEN": "malformed-inheritance-code",
        "RIGPILOT_CONTROLLER_URL": "https://wrong-controller.invalid",
        "RIGPILOT_DEVICE_NAME": "first-attempt",
    })
    assert first.returncode == 1
    assert not state.exists() and not pending.exists() and not receipt.exists()
    assert json.loads(config.read_text())["server_url"] == "https://wrong-controller.invalid"
    assert "malformed-inheritance-code" not in first.stdout + first.stderr

    corrected_url = "https://correct-controller.invalid"
    retry = run(env_tokens={
        "RIGPILOT_REPLACEMENT_TOKEN": "rpr_" + "A" * 43,
        "RIGPILOT_CONTROLLER_URL": corrected_url,
        "RIGPILOT_DEVICE_NAME": "correct-device",
    })
    assert retry.returncode == 0, retry.stderr
    assert json.loads(config.read_text())["server_url"] == corrected_url
    assert json.loads(config.read_text())["name"] == "correct-device"
    call = json.loads(receipt.read_text())
    assert call["server_url"] == corrected_url and call["name"] == "correct-device"
    assert call["replacement_env_present"] is True and call["enrollment_env_present"] is False
    assert call["call_number"] == 1
    assert state.exists()
    assert "rpr_" + "A" * 43 not in retry.stdout + retry.stderr


@pytest.mark.parametrize("journal_name,code_var,code", [
    (".state.json.enrollment-pending", "RIGPILOT_ENROLLMENT_TOKEN", "synthetic-typed-token-123456"),
    (".state.json.replacement-pending", "RIGPILOT_REPLACEMENT_TOKEN", "rpr_" + "A" * 43),
])
def test_pending_controller_mismatch_refuses_before_agent_call(
    installer_registration, journal_name, code_var, code
):
    state, pending, receipt, config, run = installer_registration
    journal = pending.with_name(journal_name)
    journal.write_text("private-original-attempt")
    original_config = config.read_bytes()
    result = run(env_tokens={
        code_var: code,
        "RIGPILOT_CONTROLLER_URL": "https://different-controller.invalid",
    })
    assert result.returncode == 1 and not receipt.exists() and not state.exists()
    assert journal.read_text() == "private-original-attempt"
    assert config.read_bytes() == original_config
    assert "bound to its existing Controller URL and device name" in result.stderr
    assert code not in result.stdout + result.stderr


def test_pending_device_name_mismatch_refuses_before_agent_call(installer_registration):
    state, pending, receipt, config, run = installer_registration
    pending.with_name(".state.json.replacement-pending").write_text("private-original-attempt")
    original_config = config.read_bytes()
    result = run(env_tokens={
        "RIGPILOT_REPLACEMENT_TOKEN": "rpr_" + "A" * 43,
        "RIGPILOT_DEVICE_NAME": "different-device",
    })
    assert result.returncode == 1 and not receipt.exists() and not state.exists()
    assert config.read_bytes() == original_config
    assert "bound to its existing Controller URL and device name" in result.stderr


@pytest.mark.parametrize("unsafe_url", [
    "", "http://controller.invalid", "https://user:secret@controller.invalid",
    "https://controller.invalid?query=1",
])
def test_preclaim_controller_correction_rejects_unsafe_url(installer_registration, unsafe_url):
    state, pending, receipt, config, run = installer_registration
    original_config = config.read_bytes()
    result = run(env_tokens={
        "RIGPILOT_REPLACEMENT_TOKEN": "rpr_" + "A" * 43,
        "RIGPILOT_CONTROLLER_URL": unsafe_url,
    })
    assert result.returncode == 1 and not receipt.exists() and not state.exists()
    assert not pending.exists() and config.read_bytes() == original_config
    if unsafe_url:
        assert unsafe_url not in result.stdout + result.stderr


def test_pending_without_config_fails_closed_before_agent_call(installer_registration):
    state, pending, receipt, config, run = installer_registration
    pending.write_text("private-original-attempt")
    config.unlink()
    result = run(env_tokens={"RIGPILOT_ENROLLMENT_TOKEN": "synthetic-typed-token-123456"})
    assert result.returncode == 1 and not receipt.exists() and not state.exists()
    assert pending.exists() and not config.exists()
    assert "no Agent configuration" in result.stderr


def test_success_exit_without_identity_does_not_complete_installation(installer_registration):
    state, pending, receipt, _config, run = installer_registration
    pending.write_text("synthetic-pending")
    result = run("zero_without_state")
    assert result.returncode == 1 and not state.exists() and pending.exists()
    assert receipt.exists()
    assert "identity was not saved" in result.stderr



def test_pending_unknown_request_can_retry_its_original_code_once(installer_registration):
    state, pending, receipt, _config, run = installer_registration
    pending.write_text("synthetic-original-bound-pending")
    result = run("retry_original", interactive=True)
    assert result.returncode == 0, result.stderr
    assert state.exists() and not pending.exists()
    call = json.loads(receipt.read_text())
    assert call["call_number"] == 2
    assert call["enrollment_env_present"] is True and call["replacement_env_present"] is False
    assert call["token_matches_typed"] is True
    assert "must be the same code; leave blank to exit" in result.stdout
    assert "One-time enrollment code:" not in result.stdout


@pytest.mark.parametrize("token", ["", "synthetic-wrong-original-token"])
def test_pending_original_code_blank_or_failure_preserves_same_journal(installer_registration, token):
    state, pending, receipt, _config, run = installer_registration
    original = "synthetic-original-bound-pending"
    pending.write_text(original)
    result = run("retry_original", interactive=True, token=token)
    assert result.returncode == 1
    assert not state.exists() and pending.read_text() == original
    call = json.loads(receipt.read_text())
    assert call["call_number"] == (1 if not token else 2)
    assert "preserve this Agent directory" in result.stderr
    assert "valid new one-time code" not in result.stderr
    assert "One-time enrollment code:" not in result.stdout


REPLACEMENT_CODE = "rpr_" + "A" * 43
NEW_CODE = "synthetic-typed-token-123456"


def test_fresh_env_code_selects_only_its_mode_without_interaction(installer_registration):
    state, pending, receipt, config, run = installer_registration
    new_result = run(env_tokens={"RIGPILOT_ENROLLMENT_TOKEN": NEW_CODE})
    assert new_result.returncode == 0, new_result.stderr
    call = json.loads(receipt.read_text())
    assert call["enrollment_env_present"] is True and call["replacement_env_present"] is False
    assert call["token_matches_typed"] is True
    assert "Registration type:" not in new_result.stdout
    assert json.loads((config.parent / "pre-helper-env.json").read_text()) == []
    assert json.loads((config.parent / "post-helper-env.json").read_text()) == []
    state.unlink()
    receipt.unlink()
    inherited = run(env_tokens={"RIGPILOT_REPLACEMENT_TOKEN": REPLACEMENT_CODE})
    assert inherited.returncode == 0, inherited.stderr
    call = json.loads(receipt.read_text())
    assert call["replacement_env_present"] is True and call["enrollment_env_present"] is False
    assert call["replacement_matches_typed"] is True
    assert "Registration type:" not in inherited.stdout
    assert json.loads((config.parent / "pre-helper-env.json").read_text()) == []
    assert json.loads((config.parent / "post-helper-env.json").read_text()) == []


@pytest.mark.parametrize("choice,token,replacement", [
    ("", NEW_CODE, False), ("1", NEW_CODE, False), ("2", REPLACEMENT_CODE, True),
])
def test_fresh_interactive_choice_uses_hidden_code_and_correct_endpoint(installer_registration, choice, token, replacement):
    state, pending, receipt, config, run = installer_registration
    result = run(interactive=True, choice=choice, token=token)
    assert result.returncode == 0, result.stderr
    call = json.loads(receipt.read_text())
    assert call["replacement_env_present"] is replacement
    assert call["enrollment_env_present"] is (not replacement)
    assert "Registration type:" in result.stdout
    assert token not in result.stdout + result.stderr


def test_both_explicit_codes_fail_before_agent_call(installer_registration):
    state, pending, receipt, config, run = installer_registration
    result = run(env_tokens={
        "RIGPILOT_ENROLLMENT_TOKEN": NEW_CODE,
        "RIGPILOT_REPLACEMENT_TOKEN": REPLACEMENT_CODE,
    })
    assert result.returncode == 1 and not receipt.exists() and not state.exists()
    assert "cannot be supplied together" in result.stderr
    assert NEW_CODE not in result.stderr and REPLACEMENT_CODE not in result.stderr


def test_both_pending_journals_fail_without_agent_call(installer_registration):
    state, pending, receipt, config, run = installer_registration
    replacement_pending = pending.with_name(".state.json.replacement-pending")
    pending.write_text("private-enrollment-journal")
    replacement_pending.write_text("private-replacement-journal")
    result = run(env_tokens={"RIGPILOT_ENROLLMENT_TOKEN": NEW_CODE})
    assert result.returncode == 1 and not receipt.exists()
    assert pending.exists() and replacement_pending.exists()
    assert "both registration and inheritance are pending" in result.stderr
    assert "private-" not in result.stderr


@pytest.mark.parametrize("pending_kind,wrong_code", [
    ("enrollment", "RIGPILOT_REPLACEMENT_TOKEN"),
    ("replacement", "RIGPILOT_ENROLLMENT_TOKEN"),
])
def test_pending_kind_cannot_be_switched_by_other_code(installer_registration, pending_kind, wrong_code):
    state, pending, receipt, config, run = installer_registration
    journal = pending if pending_kind == "enrollment" else pending.with_name(".state.json.replacement-pending")
    journal.write_text("private-original-attempt")
    supplied = REPLACEMENT_CODE if wrong_code == "RIGPILOT_REPLACEMENT_TOKEN" else NEW_CODE
    result = run(env_tokens={wrong_code: supplied})
    assert result.returncode == 1 and journal.exists() and not receipt.exists()
    assert "original" in result.stderr and "private-" not in result.stderr


def test_pending_inheritance_requires_original_code_and_reuses_attempt(installer_registration):
    state, pending, receipt, config, run = installer_registration
    journal = pending.with_name(".state.json.replacement-pending")
    journal.write_text("private-original-inheritance")
    missing = run()
    assert missing.returncode == 1 and not receipt.exists() and journal.exists()
    assert "original inheritance code" in missing.stderr
    success = run("retry_replacement", env_tokens={"RIGPILOT_REPLACEMENT_TOKEN": REPLACEMENT_CODE})
    assert success.returncode == 0, success.stderr
    call = json.loads(receipt.read_text())
    assert call["replacement_env_present"] is True and call["enrollment_env_present"] is False
    assert call["replacement_matches_typed"] is True
    assert not journal.exists() and state.exists()


@pytest.mark.parametrize("mode,token_var,token,journal_name", [
    ("new_fail_pending", "RIGPILOT_ENROLLMENT_TOKEN", NEW_CODE, ".state.json.enrollment-pending"),
    ("inherit_fail_pending", "RIGPILOT_REPLACEMENT_TOKEN", REPLACEMENT_CODE, ".state.json.replacement-pending"),
])
def test_uncertain_first_request_retains_its_mode_and_pending(installer_registration, mode, token_var, token, journal_name):
    state, pending, receipt, config, run = installer_registration
    first = run(mode, env_tokens={token_var: token})
    journal = pending.with_name(journal_name)
    assert first.returncode == 1 and journal.exists() and not state.exists()
    before = journal.read_text()
    assert "original" in first.stderr
    receipt.unlink()
    other_var = "RIGPILOT_REPLACEMENT_TOKEN" if token_var == "RIGPILOT_ENROLLMENT_TOKEN" else "RIGPILOT_ENROLLMENT_TOKEN"
    other_token = REPLACEMENT_CODE if other_var == "RIGPILOT_REPLACEMENT_TOKEN" else NEW_CODE
    wrong_mode = run(env_tokens={other_var: other_token})
    assert wrong_mode.returncode == 1 and not receipt.exists() and journal.read_text() == before
    retry_mode = "retry_original" if token_var == "RIGPILOT_ENROLLMENT_TOKEN" else "retry_replacement"
    retry = run(retry_mode, env_tokens={token_var: token})
    assert retry.returncode == 0, retry.stderr
    assert state.exists() and not journal.exists()


@pytest.fixture
def first_config_selection(tmp_path):
    source = INSTALLER.read_text()
    start = source.index("    # A pre-supplied code selects unattended first registration.")
    end = source.index('    CONFIG_STAGE="${CONFIG_DIR}/.config.json.install.$$"', start)
    block = source[start:end]
    harness = tmp_path / "config-choice-only.sh"
    harness.write_text(
        "set -Eeuo pipefail\n" + _secret_bootstrap(source)
        + "\nCONTROLLER_DEFAULT=https://controller.example.invalid\n"
        + _function(source, "fail") + "\n"
        + "python3 -c 'import os,sys; sys.exit(int(any(k in os.environ for k in (\"RIGPILOT_ENROLLMENT_TOKEN\", \"RIGPILOT_REPLACEMENT_TOKEN\", \"INSTALL_ENROLLMENT_TOKEN\", \"INSTALL_REPLACEMENT_TOKEN\"))))'\n"
        + block
        + 'printf \'controller=%s\\nname=%s\\n\' "${CONTROLLER_URL}" "${DEVICE_NAME}"\n'
    )
    def run(values):
        env = {key: value for key, value in os.environ.items() if key not in {
            "RIGPILOT_ENROLLMENT_TOKEN", "RIGPILOT_REPLACEMENT_TOKEN",
            "RIGPILOT_CONTROLLER_URL", "RIGPILOT_DEVICE_NAME",
        }}
        env.update(values)
        return subprocess.run(["bash", str(harness)], input="", capture_output=True, text=True, env=env, timeout=5)
    return run


@pytest.mark.parametrize("token_var,token", [
    ("RIGPILOT_ENROLLMENT_TOKEN", NEW_CODE),
    ("RIGPILOT_REPLACEMENT_TOKEN", REPLACEMENT_CODE),
])
def test_unattended_new_config_requires_controller_url_and_device_name(first_config_selection, token_var, token):
    values = {
        token_var: token,
        "RIGPILOT_CONTROLLER_URL": "https://controller.example.test",
        "RIGPILOT_DEVICE_NAME": "visible-device",
    }
    result = first_config_selection(values)
    assert result.returncode == 0, result.stderr
    assert "controller=https://controller.example.test" in result.stdout
    assert "name=visible-device" in result.stdout
    assert "Controller URL [" not in result.stdout
    assert token not in result.stdout + result.stderr


@pytest.mark.parametrize("overrides", [
    {"RIGPILOT_CONTROLLER_URL": ""},
    {"RIGPILOT_DEVICE_NAME": ""},
    {"RIGPILOT_DEVICE_NAME": "   "},
    {"RIGPILOT_CONTROLLER_URL": "http://unsafe.example.test"},
])
def test_unattended_new_config_rejects_missing_or_unsafe_metadata(first_config_selection, overrides):
    values = {
        "RIGPILOT_REPLACEMENT_TOKEN": REPLACEMENT_CODE,
        "RIGPILOT_CONTROLLER_URL": "https://controller.example.test",
        "RIGPILOT_DEVICE_NAME": "visible-device",
        **overrides,
    }
    result = first_config_selection(values)
    assert result.returncode != 0
    assert "controller=" not in result.stdout and "name=" not in result.stdout
    assert "Controller URL [" not in result.stdout
    assert REPLACEMENT_CODE not in result.stdout + result.stderr


def test_new_config_rejects_dual_codes_and_no_code_without_tty(first_config_selection):
    dual = first_config_selection({
        "RIGPILOT_ENROLLMENT_TOKEN": NEW_CODE,
        "RIGPILOT_REPLACEMENT_TOKEN": REPLACEMENT_CODE,
        "RIGPILOT_CONTROLLER_URL": "https://controller.example.test",
        "RIGPILOT_DEVICE_NAME": "visible-device",
    })
    assert dual.returncode != 0 and "cannot be supplied together" in dual.stderr
    missing = first_config_selection({})
    assert missing.returncode != 0 and "interactive terminal" in missing.stderr
    assert "Controller URL [" not in missing.stdout


def test_image_firstboot_saved_identity_migrates_retired_keys_without_reclaim(tmp_path):
    """Exercise the Installer's saved-config branch with image firstboot inputs."""
    import shlex

    source = INSTALLER.read_text()
    migration_start = source.index('    # Existing identity, policy and controller settings are preserved.')
    migration_end = source.index('    chown root:root "${CONFIG_STAGE}"', migration_start)
    migration = source[migration_start:migration_end]
    entry = 'if [[ ! -s "${STATE_FILE}" ]]; then\n    enroll_agent_identity\nfi\nunset INSTALL_ENROLLMENT_TOKEN INSTALL_REPLACEMENT_TOKEN'
    assert entry in source

    config = tmp_path / 'config.json'
    state = tmp_path / 'state.json'
    receipt = tmp_path / 'unexpected-enrollment-call'
    config.write_text(json.dumps({
        'server_url': 'https://synthetic-controller.invalid',
        'state_file': str(state),
        'allow_remote_sessions': True,
        'allow_remote_root_sessions': False,
        'allow_local_web': True,
        'allow_nvidia_driver_updates': True,
        'allow_hiveos_runtime_preparation': True,
        'allow_wifi_management': True,
    }))
    state.write_text('{"agent_id":"already-persisted"}')
    portable = INSTALLER.parent
    header = '\n'.join((
        'set -Eeuo pipefail',
        _secret_bootstrap(source),
        _function(source, 'fail'),
        f'CONFIG_DIR={shlex.quote(str(tmp_path))}',
        f'CONFIG_FILE={shlex.quote(str(config))}',
        f'STATE_FILE={shlex.quote(str(state))}',
        f'SOURCE_ROOT={shlex.quote(str(portable))}',
        f'enroll_agent_identity() {{ touch {shlex.quote(str(receipt))}; }}',
    )) + '\n'
    harness = tmp_path / 'saved-identity-install-parts.sh'
    harness.write_text(header + migration + '\n' + entry + '\n')
    env = {key: value for key, value in os.environ.items() if key not in {
        'RIGPILOT_ENROLLMENT_TOKEN', 'RIGPILOT_REPLACEMENT_TOKEN',
        'RIGPILOT_CONTROLLER_URL', 'RIGPILOT_DEVICE_NAME',
    }}
    result = subprocess.run(['bash', str(harness)], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, env=env, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '' and not receipt.exists()
    assert state.read_text() == '{"agent_id":"already-persisted"}'
    staged = list(tmp_path.glob('.config.json.install.*'))
    assert len(staged) == 1
    settings = json.loads(staged[0].read_text())
    assert settings['allow_agent_updates'] is True
    for retired in (
        'allow_remote_sessions',
        'allow_remote_root_sessions',
        'allow_local_web',
        'allow_nvidia_driver_updates',
        'allow_hiveos_runtime_preparation',
        'allow_wifi_management',
    ):
        assert retired not in settings
    assert settings['advertise_ssh'] is True and settings['advertise_sftp'] is True

