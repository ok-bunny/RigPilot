#!/usr/bin/env python3
"""Controller actual-archive HTTP smoke in the qualified offline container.

The service is the extracted payload's server.main:app, never a source mock.
Only synthetic users, an enrolled synthetic Agent identity, and this script's
private SQLite DB are used. No Agent/miner executable or real device is used.
"""
from pathlib import Path
import argparse
import ast
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid

OLD_PASSWORD = " \tX "  # Short bootstrap credential; every whitespace byte matters.
NEW_PASSWORD = "  synthetic-controller-密码-secret  "
LEGACY_PASSWORD = "  synthetic-controller-legacy-secret  "
OTHER_PASSWORD = "synthetic-controller-other-password"
parser = argparse.ArgumentParser()
parser.add_argument("--manifest-sha256", required=True)
parser.add_argument("--main-sha256", required=True)
parser.add_argument("--cache-version", "--cache-revision", dest="cache_version", required=True)
parser.add_argument("--root", type=Path, default=Path("/input"))
parser.add_argument("--output", type=Path, default=Path("/output"))
parser.add_argument("--port", type=int, default=18211)
parser.add_argument("--environment-note", required=True)
args = parser.parse_args()
for name in ("manifest_sha256", "main_sha256"):
    if re.fullmatch(r"[0-9a-f]{64}", getattr(args, name)) is None:
        parser.error(name + " must be the current owner-frozen SHA")
ROOT, OUTPUT = args.root.resolve(), args.output.resolve()
PORT = args.port
assert 1024 <= PORT <= 65535
DATABASE = OUTPUT / "data/controller.db"
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()


def source_verified(manifest):
    actual = {p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() or p.is_symlink()}
    assert actual == set(manifest["files"]) | {"controller-release.json"}
    for name, wanted in manifest["files"].items():
        path = ROOT / name
        assert path.is_file() and not path.is_symlink() and sha(path) == wanted, name
        if name.endswith(".py"):
            ast.parse(path.read_text(), feature_version=(3, 10))


if sys.flags.optimize:
    raise RuntimeError("smoke assertions must run without Python optimization")
assert sha(ROOT / "controller-release.json") == args.manifest_sha256
manifest = json.loads((ROOT / "controller-release.json").read_text())
VERSION = manifest["version"]
assert re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", VERSION)
assert manifest["files"]["server/main.py"] == args.main_sha256
source_verified(manifest)
target = json.loads((ROOT / "deploy/controller-runtime/runtime-target.json").read_text())
assert target["controller_version"] == VERSION
assert platform.system() == target["platform"]["system"] == "Linux"
assert platform.machine() == target["platform"]["machine"] == "x86_64"
assert platform.python_implementation() == target["python"]["implementation"] == "CPython"
assert platform.python_version() == target["python"]["validated_patch"]
assert ".".join(map(str, sys.version_info[:2])) == target["python"]["major_minor"]
assert platform.libc_ver() == ("glibc", target["platform"]["glibc_minimum"])
release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
assert release["ID"].strip('"') == "ubuntu" and release["VERSION_ID"].strip('"') == "22.04"
assert target["dependencies"]["only_binary"] is True and target["dependencies"]["require_hashes"] is True
for key in ("lock", "wheelhouse"):
    relative = Path(target["dependencies"][key])
    assert not relative.is_absolute() and ".." not in relative.parts
lock = ROOT / target["dependencies"]["lock"]
wheels = sorted((ROOT / target["dependencies"]["wheelhouse"]).glob("*.whl"))
lock_hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", lock.read_text())
assert len(wheels) == len(lock_hashes) == target["dependencies"]["wheel_count"]
assert {sha(path) for path in wheels} == set(lock_hashes)
cache_tags = re.findall(r"[?&]v=([^\"'& >]+)", (ROOT / "server/static/index.html").read_text())
assert cache_tags
CACHE = cache_tags[0]
assert CACHE == VERSION or CACHE.startswith(VERSION + "-")
assert args.cache_version == CACHE
routes = [line.split() for line in Path("/proc/net/route").read_text().splitlines()[1:] if line.split()]
assert all(row[0] == "lo" for row in routes), "offline container must have no non-loopback route"
for key in tuple(os.environ):
    if key.startswith("RIGPILOT_"):
        del os.environ[key]
os.environ.update(RIGPILOT_DATA_DIR=str(OUTPUT / "data"), RIGPILOT_DATABASE=str(DATABASE),
                  RIGPILOT_SECRET_KEY="synthetic-controller-native-smoke-secret-key",
                  RIGPILOT_ADMIN_USERNAME="admin", RIGPILOT_ADMIN_PASSWORD=OLD_PASSWORD,
                  PYTHONDONTWRITEBYTECODE="1")
sys.path.insert(0, str(ROOT))
import server
from server.auth import create_session_token
assert server.__version__ == VERSION


def request(method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=12)
    supplied = {"Accept": "application/json", **(headers or {})}
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        supplied["Content-Type"] = "application/json"
    try:
        connection.request(method, path, raw, supplied)
        response = connection.getresponse()
        return response.status, response.read(1024 * 1024)
    finally:
        connection.close()


def api(method, path, body=None, headers=None, expected=200):
    deadline = time.monotonic() + 5
    while True:
        status, content = request(method, path, body, headers)
        # No request/response secret is copied to the report, including on failure.
        assert status == expected, (method, path, status, expected)
        result = json.loads(content) if content else None
        if (method == "POST" and path == "/api/agent/heartbeat"
                and isinstance(result, dict) and result.get("stale_heartbeat") is True):
            assert time.monotonic() < deadline, "fresh synthetic heartbeat receipt timeout"
            time.sleep(.25)
            continue
        return result


def login(username="admin", password=OLD_PASSWORD):
    result = api("POST", "/api/auth/login", {"username": username, "password": password})
    return {"Authorization": "Bearer " + result["token"]}


def rows(sql, params=()):
    with closing(sqlite3.connect(DATABASE)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, params)]


def mutate(sql, params=()):
    with closing(sqlite3.connect(DATABASE)) as db:
        db.execute(sql, params)
        db.commit()


def business_snapshot():
    return {name: rows("SELECT * FROM " + name + " ORDER BY rowid")
            for name in ("agents", "wallets", "pools", "flight_sheets", "commands")}


def setup_state(admin):
    return api("GET", "/api/setup", headers=admin)


def setup_action(admin, state, action, expected=200, **changes):
    return api("PATCH", "/api/setup", {"expected_revision": state["revision"], "action": action, **changes},
               admin, expected)


def setup_fields(state):
    return {key: state[key] for key in ("status", "step", "password_changed", "revision")}


def password_body(current=OLD_PASSWORD, new=NEW_PASSWORD, **changes):
    return {"current_password": current, "new_password": new, "confirm_password": new, **changes}


def no_secret_text(content):
    text = content.decode() if isinstance(content, bytes) else json.dumps(content, ensure_ascii=False)
    for secret in (NEW_PASSWORD, LEGACY_PASSWORD, OTHER_PASSWORD, "synthetic-forged-setup-secret"):
        assert secret not in text
    assert "pbkdf2_sha256$" not in text


def account_and_fresh_setup_http():
    admin, second = login(), login()
    before_business = business_snapshot()
    state = setup_state(admin)
    assert setup_fields(state) == {"status": "pending", "step": "password", "password_changed": False, "revision": 1}
    assert isinstance(state["updated_at"], str) and state["updated_at"]
    assert setup_state(admin) == state and business_snapshot() == before_business
    before_user = rows("SELECT * FROM users WHERE username='admin'")[0]
    legacy_token = create_session_token({"uid": before_user["id"], "sub": "admin", "role": "admin"},
                                       os.environ["RIGPILOT_SECRET_KEY"], 3600)
    legacy_auth = {"Authorization": "Bearer " + legacy_token}
    api("GET", "/api/auth/me", headers=legacy_auth)
    api("GET", "/api/setup", expected=401)
    api("PATCH", "/api/setup", {"expected_revision": 1, "action": "later"}, expected=401)
    api("POST", "/api/auth/password", password_body(), expected=401)
    other = None
    for role in ("viewer", "operator"):
        api("POST", "/api/users", {"username": role, "password": OTHER_PASSWORD, "role": role}, admin, 201)
        role_auth = login(role, OTHER_PASSWORD)
        api("POST", "/api/auth/password", password_body(current=OTHER_PASSWORD), role_auth, 403)
        api("GET", "/api/setup", headers=role_auth, expected=403)
        api("PATCH", "/api/setup", {"expected_revision": 1, "action": "later"}, role_auth, 403)
        if role == "viewer":
            other = role_auth
    invalid = [password_body(confirm_password="synthetic-mismatched-confirmation"),
               password_body(new="too-short"), password_body(user_id=before_user["id"]),
               password_body(current=""), password_body(current="x" * 257)]
    for body in invalid:
        rejected = api("POST", "/api/auth/password", body, admin, 422)
        no_secret_text(rejected)
        serialized = json.dumps(rejected, ensure_ascii=False)
        for key in ("new_password", "confirm_password"):
            assert body[key] not in serialized
    api("POST", "/api/auth/password", password_body(current="synthetic-wrong-current"), admin, 400)
    assert rows("SELECT * FROM users WHERE username='admin'")[0] == before_user
    assert setup_state(admin) == state
    api("GET", "/api/auth/me", headers=admin)
    # Skip/later are honest choices: completing guidance does not claim a password change.
    skipped = setup_action(admin, state, "skip")
    assert skipped["step"] == "device" and skipped["password_changed"] is False
    deferred = setup_action(admin, skipped, "later")
    assert deferred["status"] == "deferred" and deferred["step"] == "device"
    assert setup_state(login()) == deferred
    reopened = setup_action(admin, deferred, "reopen")
    assert reopened["status"] == "pending" and reopened["step"] == "device"
    configuration = setup_action(admin, reopened, "skip")
    assert configuration["step"] == "configuration"
    complete = setup_action(admin, configuration, "skip")
    assert complete["status"] == "complete" and complete["step"] == "done" and complete["password_changed"] is False
    state = setup_action(admin, complete, "reopen")
    assert state["step"] == "password" and state["password_changed"] is False
    rejected = setup_action(admin, state, "later", 422, password_changed=True,
                            secret="synthetic-forged-setup-secret")
    no_secret_text(rejected)
    assert setup_state(admin) == state
    assert business_snapshot() == before_business
    # Force only this private SQLite setup write to fail. Real HTTP500 must roll
    # back hash, SV and setup together; no alternate app or endpoint is injected.
    before_transaction = rows("SELECT * FROM users WHERE username='admin'")[0]
    mutate("""CREATE TRIGGER synthetic_smoke_setup_failure BEFORE UPDATE ON controller_preferences
              WHEN OLD.key='installation_setup'
              BEGIN SELECT RAISE(ABORT,'synthetic setup write failure'); END""")
    try:
        status, _content = request("POST", "/api/auth/password", password_body(), admin)
        assert status == 500
        assert rows("SELECT * FROM users WHERE username='admin'")[0] == before_transaction
        assert setup_state(admin) == state
        api("GET", "/api/auth/me", headers=second)
    finally:
        mutate("DROP TRIGGER synthetic_smoke_setup_failure")
    before_other = rows("SELECT * FROM users WHERE username='viewer'")[0]
    changed = api("POST", "/api/auth/password", password_body(), admin)
    assert changed == {"changed": True, "reauthenticate": True}
    no_secret_text(changed)
    after_user = rows("SELECT * FROM users WHERE username='admin'")[0]
    assert after_user["password_hash"] != before_user["password_hash"]
    assert after_user["session_version"] == before_user["session_version"] + 1
    for old in (admin, second, legacy_auth):
        api("GET", "/api/auth/me", headers=old, expected=401)
    api("GET", "/api/setup", headers=admin, expected=401)
    api("GET", "/api/auth/me", headers=other)
    assert rows("SELECT * FROM users WHERE username='viewer'")[0] == before_other
    api("POST", "/api/auth/login", {"username": "admin", "password": OLD_PASSWORD}, expected=401)
    api("POST", "/api/auth/login", {"username": "admin", "password": NEW_PASSWORD.strip()}, expected=401)
    fresh = login(password=NEW_PASSWORD)
    api("GET", "/api/auth/me", headers=fresh)
    resumed = setup_state(fresh)
    assert resumed["status"] == "pending" and resumed["step"] == "device" and resumed["password_changed"] is True
    assert resumed["revision"] == state["revision"] + 1
    setup_action(fresh, state, "step", 409, step="password")
    assert setup_state(fresh) == resumed
    configuration = setup_action(fresh, resumed, "step", step="configuration")
    complete = setup_action(fresh, configuration, "complete")
    assert complete["status"] == "complete" and complete["step"] == "done"
    reopened = setup_action(fresh, complete, "reopen")
    assert reopened["step"] == "device" and reopened["password_changed"] is True
    deferred = setup_action(fresh, reopened, "later")
    assert deferred["status"] == "deferred" and deferred["step"] == "device"
    assert business_snapshot() == before_business
    audits = "\n".join(row["details_json"] for row in rows("SELECT details_json FROM audit_logs"))
    preferences = "\n".join(row["value"] for row in rows("SELECT value FROM controller_preferences"))
    for text in (audits, preferences):
        no_secret_text(text)
        assert OLD_PASSWORD not in text
    return fresh, other, admin, deferred, after_user, {
        "fresh_pending_three_page_progress": True, "setup_GET_idempotent_no_business_objects_or_commands": True,
        "skip_later_complete_reopen_without_false_password_fact": True,
        "unauthenticated_and_non_admin_password_setup_rejected": True,
        "confirm_length_target_field_and_wrong_old_rejected_without_mutation": True,
        "raw_short_whitespace_bootstrap_and_whitespace_Unicode_new_password": True,
        "password_hash_SV_setup_atomic_success_and_forced_SQLite_failure_rollback": True,
        "old_two_SV_sessions_and_legacy_no_SV_session_revoked_other_user_unchanged": True,
        "reauthentication_resumes_device_and_late_tab_CAS_409": True,
        "deferred_progress_persisted_for_restart": True,
        "no_password_hash_or_plaintext_in_response_audit_or_preferences": True,
    }


def synthetic_agent(admin, label="synthetic-controller-unsupported", version=VERSION):
    token = api("POST", "/api/enrollment-tokens", {"label": label, "ttl_minutes": 5}, admin)
    identity = api("POST", "/api/agent/enroll", {"enrollment_token": token["token"], "name": label,
        "hostname": label, "os_family": "linux", "agent_version": version}, expected=201)
    auth = {"X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"]}
    heartbeat = {"hostname": label, "os_family": "linux", "agent_version": version,
                 "metrics": {}, "inventory": {}}
    assert api("POST", "/api/agent/heartbeat", heartbeat, auth)["commands"] == []
    return identity["agent_id"], auth, heartbeat


def gpu_oc_failure_http(admin):
    # Synthetic receipts exercise the packaged Controller consumer only. The
    # actual Agent producer/backend contract is verified by its scoped tests.
    agent_id, agent_auth, heartbeat = synthetic_agent(admin, "synthetic-gpu-oc", "0.8.48")
    gpu_uuid = "GPU-synthetic-controller-oc-0"
    heartbeat["metrics"] = {
        "mining_status": {"configured": True, "state": "running", "miner_type": "generic",
                          "flight_sheet_id": "synthetic-oc-flight", "process_verified": True},
        "gpus": [{"index": index, "uuid": gpu_uuid if index == 0 else "GPU-synthetic-controller-oc-1",
                  "vendor": "nvidia", "name": "NVIDIA synthetic HTTP GPU",
                  "oc_capabilities": ["core_clock_offset_mhz"]} for index in (0, 1)],
    }
    path = "/api/agents/" + agent_id
    body = {"vendor": "nvidia", "gpu_uuid": gpu_uuid,
            "setting": {"index": 0, "core_clock_offset_mhz": 500}}

    def poll():
        return api("POST", "/api/agent/heartbeat", heartbeat, agent_auth)["commands"]

    def desired():
        entries = api("GET", path, headers=admin)["desired_gpu_oc"]
        assert len(entries) == 1 and entries[0]["gpu_uuid"] == gpu_uuid
        assert entries[0]["setting"] == body["setting"]
        return entries[0]

    def save():
        queued = api("POST", path + "/gpu-oc/apply", body, admin, 201)
        public = api("GET", "/api/commands/" + queued["id"], headers=admin)
        assert "_controller_gpu_oc_intent" not in public["payload"]
        return queued

    def dispatch(queued):
        commands = poll()
        assert len(commands) == 1 and commands[0]["id"] == queued["id"]
        assert commands[0]["action"] == "apply_gpu_profile"
        assert commands[0]["payload"]["expected_gpu_uuid"] == gpu_uuid
        assert commands[0]["payload"]["settings"] == [body["setting"]]
        assert "_controller_gpu_oc_intent" not in commands[0]["payload"]
        return commands[0]

    def receipt(command, status, result, error=None):
        return api("POST", "/api/agent/commands/" + command["id"] + "/result",
                   {"status": status, "result": result, "error": error}, agent_auth)

    def r2_fault(command, code, early_identity=False):
        payload = command["payload"]
        setting = {key: int(value) if isinstance(value, float) and value.is_integer() else value
                   for key, value in payload["settings"][0].items()}
        mode = payload["mode"]
        requested_uuid = payload["expected_gpu_uuid"]

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()

        # expected_gpu_uuid in canonical hashes is the requested intention,
        # never positive hardware identity proof, including early UUID failure.
        recovery = {
            "status": "partial", "confirmed_reset": [], "hardware_fault": True,
            "hardware_reset_required": code == 16, "reset_required": code == 16,
            "nvml_codes": [code], "write_attempted": False, "state_reconciled": True,
            "recovered": False, "errors": [], "command_id": command["id"],
            "request_fingerprint": digest({"vendor": "nvidia", "mode": mode,
                "settings": [setting], "expected_gpu_uuid": requested_uuid}),
            "targets": [{"index": setting["index"], "requested_gpu_uuid": requested_uuid,
                "identity_verified": False, "setting_fingerprint": digest({"vendor": "nvidia",
                    "mode": mode, "index": setting["index"], "setting": setting,
                    "expected_gpu_uuid": requested_uuid})}],
            "failure_target": {"index": setting["index"], "operation": "query",
                "requested_gpu_uuid": requested_uuid, "identity_verified": False},
        }
        if early_identity:
            recovery.update(phase="identity_verification", identity_verified=False, state_unchanged=True)
            recovery["failure_target"]["phase"] = "identity_verification"
        else:
            recovery["failure_target"]["field"] = "core_clock_offset_mhz"
        return {"gpu_oc_recovery": recovery}

    assert poll() == []
    command = dispatch(save())
    saved = desired()
    receipt(command, "succeeded", {"vendor": "nvidia",
        "gpu_profile_id": command["payload"]["gpu_profile_id"],
        "operations": [{"index": 0, "applied": ["core_clock_offset_mhz"]}], "reset": []})
    restart = api("POST", path + "/commands", {"action": "restart_mining", "payload": {}}, admin, 201)
    commands = poll()
    assert len(commands) == 1 and commands[0]["id"] == restart["id"]
    receipt(restart, "succeeded", {"restarted": True})
    commands = poll()
    assert len(commands) == 1 and commands[0]["action"] == "apply_gpu_profile"
    failed_command = commands[0]
    assert "_controller_gpu_oc_intent" not in failed_command["payload"]
    fault = r2_fault(failed_command, 16)
    receipt(failed_command, "failed", fault, "query GPU 0 core clock offset limits failed: GPU requires reset")
    held = desired()
    assert held["intent_id"] == saved["intent_id"]
    assert held["replay_block"]["intent_id"] == held["intent_id"]
    assert held["replay_block"]["reason"] == "hardware_reset_required"
    assert held["replay_block"]["hardware_reset_required"] is True
    assert held["replay_block"]["hardware_identity_confirmed"] is False
    assert poll() == [] and desired() == held

    queued = save()
    newer = desired()
    assert newer["intent_id"] != held["intent_id"] and "replay_block" not in newer
    receipt(failed_command, "failed", fault, "late duplicate synthetic NVML16 receipt")
    assert desired() == newer
    command = dispatch(queued)
    assert desired() == newer
    receipt(command, "failed", r2_fault(command, 15, early_identity=True), "synthetic NVML15 GPU lost")
    held = desired()
    assert held["replay_block"]["reason"] == "hardware_fault"
    assert held["replay_block"]["nvml_codes"] == [15]
    assert held["replay_block"]["hardware_reset_required"] is False
    assert held["replay_block"]["hardware_identity_confirmed"] is False
    assert poll() == []

    # Real047 text remains supported, but a fault naming another GPU cannot
    # block this card. These are terminal synthetic receipts, not GPU actions.
    heartbeat["agent_version"] = "0.8.47"
    command = dispatch(save())
    receipt(command, "failed", {}, "set GPU 1 core_clock_offset_mhz failed: GPU requires reset")
    assert "replay_block" not in desired()
    command = dispatch(save())
    legacy_error = ("query GPU 0 core clock offset limits failed: replacement query: GPU requires reset; "
                    "legacy query: GPU requires reset")
    receipt(command, "failed", {}, legacy_error)
    held = desired()
    assert held["replay_block"]["reason"] == "legacy_reset_required"
    assert held["replay_block"]["error"] == legacy_error
    assert held["replay_block"]["recovered"] is False and poll() == []
    assert api("GET", "/api/commands/" + command["id"], headers=admin)["error"] == legacy_error
    checks = {
        "save_success_restart_auto_reapply_NVML16_hold_preserves_saved_setting": True,
        "same_value_explicit_save_mints_new_intent_old_terminal_receipt_cannot_block_it": True,
        "R2_requested_UUID_canonical_hash_and_early_NVML15_hold_without_reset_claim": True,
        "047_same_GPU_exact_reset_text_holds_other_GPU_text_does_not": True,
        "private_command_intent_marker_not_in_admin_or_Agent_wire": True,
        "synthetic_GPU_values_only_no_Agent_GPU_or_miner_execution": True,
    }
    return checks, {"agent_id": agent_id, "auth": agent_auth, "heartbeat": heartbeat, "desired": held}


def gpu_oc_persisted_restart_http(context):
    admin = login(password=NEW_PASSWORD)
    path = "/api/agents/" + context["agent_id"]
    assert api("GET", path, headers=admin)["desired_gpu_oc"] == [context["desired"]]
    assert api("POST", "/api/agent/heartbeat", context["heartbeat"], context["auth"])["commands"] == []
    assert api("GET", path, headers=admin)["desired_gpu_oc"] == [context["desired"]]
    return {"saved_setting_intent_and_fault_hold_survive_actual_Controller_process_restart": True,
            "fresh_heartbeat_after_restart_does_not_replay_blocked_OC": True}


def unsupported_http(admin):
    unknown = "synthetic-unsupported-miner"
    wallet = api("POST", "/api/wallets", {"name": "Synthetic archive wallet", "coin": "XMR",
        "address": "synthetic-controller-public-wallet"}, admin, 201)
    body = {"name": "Synthetic historical archive flight", "miner_type": "generic", "coin": "XMR",
            "wallet_id": wallet["id"], "pool_url": "stratum+tcp://pool.example.test:34567", "algorithm": "randomx"}
    sheet = api("POST", "/api/flight-sheets", body, admin, 201)
    agent_id, agent_auth, heartbeat = synthetic_agent(admin)
    before_commands = rows("SELECT * FROM commands WHERE agent_id=?", (agent_id,))
    for method, path in (("POST", "/api/flight-sheets"), ("PUT", "/api/flight-sheets/" + sheet["id"])):
        api(method, path, {**body, "miner_type": unknown}, admin, 422)
    apply_path = "/api/agents/" + agent_id + "/flight-sheets/" + sheet["id"] + "/apply"
    mutate("UPDATE flight_sheets SET miner_type=? WHERE id=?", (unknown, sheet["id"]))
    listed = next(row for row in api("GET", "/api/flight-sheets", headers=admin) if row["id"] == sheet["id"])
    assert listed["miner_type"] == unknown
    api("POST", apply_path, headers=admin, expected=409)
    assert rows("SELECT * FROM commands WHERE agent_id=?", (agent_id,)) == before_commands
    command_path = "/api/agents/" + agent_id + "/commands"
    identities = []

    def observed(where):
        current = deepcopy(heartbeat)
        mining = {"configured": True, "state": "stopped", "process_verified": True,
                  "process_error": None, "flight_sheet_id": sheet["id"],
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        if where == "top_level":
            mining["miner_type"] = unknown
        elif where == "primary":
            mining.update(miner_type="generic", primary={"miner_type": unknown})
        elif where == "secondary":
            mining.update(miner_type="generic", primary={"miner_type": "generic"},
                          secondary={"miner_type": unknown})
        elif where == "supported":
            mining.update(miner_type="generic", primary={"miner_type": "generic"})
        else:
            assert where == "sheet"
        current["inventory"]["mining_status"] = mining
        return api("POST", "/api/agent/heartbeat", current, agent_auth)

    for where in ("sheet", "top_level", "primary", "secondary"):
        if where != "sheet":
            mutate("UPDATE flight_sheets SET miner_type='generic' WHERE id=?", (sheet["id"],))
        assert observed(where)["commands"] == []
        api("POST", command_path, {"action": "restart_mining", "payload": {}}, admin, 409)
        assert rows("SELECT * FROM commands WHERE agent_id=?", (agent_id,)) == before_commands
        identities.append(where)
    # Queuing from an explicit supported local config remains allowed. If the
    # next heartbeat reports an unknown local config, dispatch fails in place.
    observed("supported")
    queued = api("POST", command_path, {"action": "restart_mining", "payload": {}}, admin, 201)
    saved = rows("SELECT payload_json FROM commands WHERE id=?", (queued["id"],))[0]
    delivered = observed("top_level")
    assert not any(item["id"] == queued["id"] for item in delivered["commands"])
    stored = rows("SELECT status,payload_json,finished_at,error FROM commands WHERE id=?", (queued["id"],))[0]
    assert stored["status"] == "failed" and stored["finished_at"] and stored["error"]
    assert stored["payload_json"] == saved["payload_json"]
    return {"generic_unknown_create_update_and_saved_apply_rejected": True,
            "old_flight_remains_readable_without_automatic_migration": True,
            "restart_rejected_before_queue_identity_locations": identities,
            "queued_restart_failed_before_dispatch_preserves_payload": True,
            "synthetic_Agent_HTTP_only_no_Agent_or_miner_binary": True}


def workload_counter_ack_http(admin):
    agent_id, agent_auth, heartbeat = synthetic_agent(admin)
    moment = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot = {"coin": "XMR", "algorithm": "randomx", "counter_epoch": "native-ack-first",
                "snapshot_seq": 1, "estimated_work_total": "100.00", "observed_at": moment.isoformat()}
    fields = {"coin", "algorithm", "counter_epoch", "snapshot_seq", "estimated_work_total", "observed_at"}

    def submit(counter):
        # Workload counters belong to the actual top-level heartbeat schema.
        return api("POST", "/api/agent/heartbeat", {**heartbeat, "workload_counters": [counter]}, agent_auth)

    def stored_ack():
        value = rows("""SELECT coin,algorithm,counter_epoch,snapshot_seq,last_total,last_observed_at
                        FROM device_work_state WHERE agent_id=? AND coin='XMR' AND algorithm='randomx'""", (agent_id,))
        assert len(value) == 1
        value = value[0]
        return {"coin": value["coin"], "algorithm": value["algorithm"], "counter_epoch": value["counter_epoch"],
                "snapshot_seq": value["snapshot_seq"], "estimated_work_total": str(value["last_total"]),
                "observed_at": str(value["last_observed_at"])}

    first = submit(snapshot)["workload_counter_acks"]
    assert first == [stored_ack()] and set(first[0]) == fields
    assert type(first[0]["snapshot_seq"]) is int
    assert Decimal(first[0]["estimated_work_total"]) == Decimal(snapshot["estimated_work_total"])
    assert datetime.fromisoformat(first[0]["observed_at"]) == moment
    assert submit(snapshot)["workload_counter_acks"] == first
    for conflict in ({**snapshot, "snapshot_seq": 0},
                     {**snapshot, "estimated_work_total": "101"},
                     {**snapshot, "observed_at": (moment + timedelta(seconds=1)).isoformat()}):
        assert submit(conflict)["workload_counter_acks"] == []
        assert stored_ack() == first[0]
    replacement = {**snapshot, "counter_epoch": "native-ack-replacement", "snapshot_seq": 0,
                   "estimated_work_total": "0", "observed_at": (moment + timedelta(seconds=2)).isoformat()}
    assert submit(replacement)["workload_counter_acks"] == [stored_ack()]
    retired = {**snapshot, "snapshot_seq": 2, "estimated_work_total": "200",
               "observed_at": (moment + timedelta(seconds=3)).isoformat()}
    assert submit(retired)["workload_counter_acks"] == []
    assert stored_ack()["counter_epoch"] == replacement["counter_epoch"]
    assert rows("SELECT counter_epoch FROM device_work_retired_epochs WHERE agent_id=?", (agent_id,)) == [
        {"counter_epoch": snapshot["counter_epoch"]}]
    # Model only a newer receipt in this private DB. A stale accepted-HTTP
    # response must not claim an ACK; avoid api()'s normal fresh-receipt retry.
    last_seen = rows("SELECT last_seen FROM agents WHERE id=?", (agent_id,))[0]["last_seen"]
    mutate("UPDATE agents SET last_seen=? WHERE id=?", ("2099-01-01T00:00:00+00:00", agent_id))
    try:
        status, content = request("POST", "/api/agent/heartbeat",
                                  {**heartbeat, "workload_counters": [replacement]}, agent_auth)
        stale = json.loads(content)
        assert status == 200 and stale["stale_heartbeat"] is True and stale["workload_counter_acks"] == []
    finally:
        mutate("UPDATE agents SET last_seen=? WHERE id=?", (last_seen, agent_id))
    return {"actual_top_level_heartbeat_counter_protocol": True,
            "first_and_unchanged_repeat_ack_complete_six_stored_fields": True,
            "sequence_total_and_time_conflicts_not_acknowledged_or_advanced": True,
            "retired_epoch_not_acknowledged_or_restored": True,
            "private_DB_newer_receipt_stale_heartbeat_explicit_empty_ack": True}


def remote_agent(admin, label):
    token = api("POST", "/api/enrollment-tokens", {"label": label, "ttl_minutes": 5}, admin)
    identity = api("POST", "/api/agent/enroll", {"enrollment_token": token["token"], "name": label,
        "hostname": label, "os_family": "linux", "agent_version": VERSION}, expected=201)
    agent_auth = {"X-Agent-ID": identity["agent_id"], "Authorization": "Bearer " + identity["agent_secret"]}
    heartbeat = {"hostname": label, "os_family": "linux", "agent_version": VERSION,
                 "capabilities": [],
                 "metrics": {}, "inventory": {}}
    api("POST", "/api/agent/heartbeat", heartbeat, agent_auth)
    return identity["agent_id"], agent_auth, heartbeat


def retired_surface_http(admin):
    """0.8.50 removed the orphan GPU-profile CRUD, the bulk history wipe and
    their tables; every retired route must answer 404 on the real package."""
    api("GET", "/api/gpu-profiles", headers=admin, expected=404)
    api("POST", "/api/gpu-profiles", {"name": "gone", "vendor": "nvidia",
        "settings": [{"index": 0, "power_limit_w": 120}]}, admin, expected=404)
    api("DELETE", "/api/gpu-profiles/legacy-profile", headers=admin, expected=404)
    api("POST", "/api/agents/synthetic/gpu-profiles/legacy-profile/apply",
        headers=admin, expected=404)
    api("DELETE", "/api/operations/history", headers=admin, expected=404)
    tables = {row["name"] for row in rows(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    retired = {"gpu_profiles", "remote_sessions", "hiveos_package_update_imports",
               "hiveos_flight_imports", "nvidia_driver_cache_sets",
               "nvidia_driver_cache_objects", "nvidia_driver_cache_members",
               "nvidia_driver_cache_jobs", "nvidia_driver_update_cache_bindings"}
    assert not retired & tables, sorted(retired & tables)
    package_columns = {row["name"] for row in rows("PRAGMA table_info(miner_packages)")}
    assert not {"hiveos_custom_json", "hiveos_analysis_json", "upstream_url"} & package_columns
    return {"retired_routes_404": True, "retired_tables_absent": True,
            "dead_miner_package_columns_absent": True}


def agent_identity_lifecycle_http(admin):
    """Replacement/disable/delete identity guards without remote sessions.

    Controller 0.8.48 removed the web terminal/file relay; these checks keep
    the identity lifecycle assertions that used to ride on that transport.
    """
    agent_id, old_auth, heartbeat = remote_agent(admin, "synthetic-identity-lifecycle")
    agent_path = "/api/agents/" + agent_id
    api("PATCH", agent_path, {"disabled": True}, admin)
    api("POST", "/api/agent/heartbeat", heartbeat, old_auth, 401)
    api("PATCH", agent_path, {"disabled": False}, admin)
    api("POST", "/api/agent/heartbeat", heartbeat, old_auth)
    mutate("UPDATE agents SET last_seen=? WHERE id=?", ("2000-01-01T00:00:00+00:00", agent_id))
    token = api("POST", agent_path + "/replacement-token",
                {"confirm_name": "synthetic-identity-lifecycle", "ttl_minutes": 5}, admin, 201)
    new_secret = "rpa_" + secrets.token_urlsafe(36)
    inherited = api("POST", "/api/agent/replace", {"replacement_token": token["token"],
        "request_id": uuid.uuid4().hex, "proposed_agent_secret": new_secret,
        "hostname": "synthetic-replacement", "os_family": "linux", "agent_version": VERSION}, expected=201)
    assert inherited["agent_id"] == agent_id and inherited["inherited_existing_device"] is True
    api("POST", "/api/agent/heartbeat", heartbeat, old_auth, 401)
    new_auth = {"X-Agent-ID": agent_id, "Authorization": "Bearer " + new_secret}
    api("POST", "/api/agent/heartbeat", {**heartbeat, "hostname": "synthetic-replacement"}, new_auth)
    mutate("UPDATE agents SET last_seen=? WHERE id=?", ("2000-01-01T00:00:00+00:00", agent_id))
    api("DELETE", agent_path, headers=admin)
    api("POST", "/api/agent/heartbeat", heartbeat, new_auth, 401)
    return {"disable_blocks_heartbeat_reenable_restores_it": True,
            "replacement_rotates_identity_and_old_secret_is_revoked": True,
            "delete_revokes_current_identity": True}


def static_http(admin):
    assets = []
    text_by_name = {}
    for route, name in (("/", "index.html"), ("/static/app.js", "app.js"),
                       ("/static/pci-identity-data.js", "pci-identity-data.js"), ("/static/styles.css", "styles.css"),
                       ("/static/theme-init.js", "theme-init.js")):
        status, content = request("GET", route)
        path = "server/static/" + name
        assert status == 200 and hashlib.sha256(content).hexdigest() == manifest["files"][path]
        text_by_name[name] = content.decode()
        if name.endswith(".html"):
            revisions = re.findall(rb"[?&]v=([^\"'& >]+)", content)
            assert revisions and all(item.decode() == CACHE or item.decode().startswith(CACHE + "-") for item in revisions)
            if name == "index.html":
                bootstrap = re.search(rb'<script id="minerAdapterBootstrap" type="application/json">(.*?)</script>', content, re.S)
                assert bootstrap and json.loads(bootstrap.group(1)) == api("GET", "/api/miner-adapters", headers=admin)
        assets.append({"route": route, "path": path, "sha256": hashlib.sha256(content).hexdigest()})
    html, app = text_by_name["index.html"], text_by_name["app.js"]
    for name in ("passwordChangeDialog", "passwordChangeForm", "passwordChangeCurrent", "passwordChangeNew",
                 "passwordChangeConfirm", "setupDialog", "setupPasswordButton", "openSetupButton"):
        assert 'id="' + name + '"' in html
    assert 'data-setup-step="password"' in html and 'data-setup-step="device"' in html and 'data-setup-step="configuration"' in html
    assert len(re.findall(r'data-setup-panel="', html)) == 3
    assert re.search(r'id="setupPasswordButton"[^>]*class="btn btn-primary"', html)
    assert re.search(r'id="setupNextButton"[^>]*class="btn btn-secondary"', html)
    assert re.search(r'id="setupPreviousButton"[^>]*\bdisabled\b', html)
    assert "建议先更改管理员密码，使用自己保管的密码登录控制端。" in html
    assert "修改后需重新登录，向导会从设备接入继续。" in html
    assert 'api("/api/auth/password"' in app and 'api("/api/setup"' in app
    assert "登录已失效；若刚修改密码，请使用新密码登录。" in app
    return assets, {"actual_HTTP_bytes_equal_manifest": True, "all_HTML_cache_tags_match_release": True,
                    "bootstrap_catalog_matches_API": True, "three_setup_panels_and_password_form_present": True,
                    "password_primary_skip_secondary_previous_disabled": True,
                    "approved_Chinese_copy_and_expired_login_race_notice_present": True,
                    "generic_bootstrap_API_exact_identity_consistency": True,
                    "layout_and_browser_behavior": "reuse accepted source desktop/mobile evidence; byte presence is not a new browser claim"}


def persisted_restart_http(old_session, other, prior_setup, prior_user):
    # The original bootstrap environment is unchanged. This is a real process
    # restart, not create_app() against a source fixture.
    assert rows("SELECT * FROM users WHERE username='admin'")[0] == prior_user
    api("POST", "/api/auth/login", {"username": "admin", "password": OLD_PASSWORD}, expected=401)
    admin = login(password=NEW_PASSWORD)
    assert setup_state(admin) == prior_setup
    api("GET", "/api/auth/me", headers=old_session, expected=401)
    api("GET", "/api/auth/me", headers=other)
    return {"same_database_real_process_restart": True, "bootstrap_env_does_not_reset_password_or_SV": True,
            "deferred_setup_persists_after_restart": True, "old_session_still_revoked_other_user_still_valid": True}


def prepare_old_schema_clone():
    global DATABASE
    # The first database has already passed a real restart with its new SV.
    # Clone it only while stopped, then deliberately model an older users table
    # in this second private database. No production DB is read or copied.
    legacy_dir = OUTPUT / "data-legacy"
    legacy_dir.mkdir(mode=0o700)
    legacy_database = legacy_dir / "controller.db"
    assert not legacy_database.exists()
    with closing(sqlite3.connect(DATABASE)) as source, closing(sqlite3.connect(legacy_database)) as destination:
        source.backup(destination)
    DATABASE = legacy_database
    mutate("DELETE FROM controller_preferences WHERE key='installation_setup'")
    mutate("ALTER TABLE users DROP COLUMN session_version")
    columns = {row["name"] for row in rows("PRAGMA table_info(users)")}
    assert "session_version" not in columns
    users = rows("SELECT * FROM users ORDER BY id")
    os.environ.update(RIGPILOT_DATA_DIR=str(legacy_dir), RIGPILOT_DATABASE=str(DATABASE))
    return users


def old_schema_upgrade_http(prior_users):
    # A real startup must ALTER the old users table. Existing account bytes are
    # preserved and initialized to SV0, which also accepts a legacy no-SV token.
    columns = {row["name"] for row in rows("PRAGMA table_info(users)")}
    assert "session_version" in columns
    current_users = rows("SELECT * FROM users ORDER BY id")
    assert [{key: value for key, value in row.items() if key != "session_version"}
            for row in current_users] == prior_users
    assert all(row["session_version"] == 0 for row in current_users)
    admin_id = next(row["id"] for row in current_users if row["username"] == "admin")
    legacy_token = create_session_token({"uid": admin_id, "sub": "admin", "role": "admin"},
                                       os.environ["RIGPILOT_SECRET_KEY"], 3600)
    old_session = {"Authorization": "Bearer " + legacy_token}
    api("GET", "/api/auth/me", headers=old_session)
    api("POST", "/api/auth/login", {"username": "admin", "password": OLD_PASSWORD}, expected=401)
    admin = login(password=NEW_PASSWORD)
    other = login("viewer", OTHER_PASSWORD)
    # Legacy GET and even self-password change must not manufacture pending
    # guidance. Only an explicit manual reopen may opt into guidance.
    prefs = rows("SELECT * FROM controller_preferences ORDER BY key")
    business = business_snapshot()
    legacy = setup_state(admin)
    assert legacy == {"status": "complete", "step": "done", "password_changed": False,
                      "revision": 0, "updated_at": None}
    assert setup_state(admin) == legacy
    assert rows("SELECT * FROM controller_preferences ORDER BY key") == prefs
    changed = api("POST", "/api/auth/password", password_body(current=NEW_PASSWORD, new=LEGACY_PASSWORD), admin)
    assert changed == {"changed": True, "reauthenticate": True}
    api("GET", "/api/auth/me", headers=admin, expected=401)
    api("GET", "/api/auth/me", headers=old_session, expected=401)
    fresh = login(password=LEGACY_PASSWORD)
    assert setup_state(fresh) == legacy
    assert rows("SELECT * FROM controller_preferences ORDER BY key") == prefs
    assert business_snapshot() == business
    api("GET", "/api/auth/me", headers=other)
    return {"isolated_synthetic_old_schema_clone_real_payload_startup_migration": True,
            "users_session_version_absent_before_and_added_on_startup": True,
            "existing_users_hashes_roles_and_fields_preserved_initialized_SV0": True,
            "legacy_no_SV_token_valid_at_0_revoked_after_self_password_change": True,
            "legacy_missing_key_GET_no_write_returns_complete_done": True,
            "legacy_self_password_change_does_not_create_pending_key": True,
            "legacy_no_setup_business_or_command_side_effects": True}


def interrupted(_number, _frame):
    raise KeyboardInterrupt("native smoke interrupted")


signal.signal(signal.SIGTERM, interrupted)
processes = []


def start_service(label):
    log = (OUTPUT / ("uvicorn-" + label + ".log")).open("xb")
    process = subprocess.Popen([sys.executable, "-B", "-m", "uvicorn", "server.main:app", "--host", "127.0.0.1",
        "--port", str(PORT), "--log-level", "error"], cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT)),
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    processes.append((label, process, log))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        assert process.poll() is None, "Uvicorn exited before health"
        try:
            health = api("GET", "/api/health")
            assert health["version"] == VERSION and health["enrollment_protocol"] == "rigpilot.enrollment.v2"
            return process
        except (OSError, AssertionError):
            time.sleep(.25)
    raise RuntimeError("Uvicorn health timeout")


def stop_service(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


failure, stable = None, False
passed, assets = [], []
account_checks, unsupported_checks, static_checks, restart_checks, upgrade_checks = {}, {}, {}, {}, {}
ack_checks, lifecycle_checks, oc_checks, oc_restart_checks, retired_checks = {}, {}, {}, {}, {}
try:
    process = start_service("fresh")
    passed.append("actual_offline_archive_bootstrap_version_login")
    admin, other, old_session, deferred, after_user, account_checks = account_and_fresh_setup_http()
    passed.append("password_current_user_roles_whitespace_confirm_atomic_hash_SV_setup_and_session_revocation")
    passed.append("fresh_three_page_setup_skip_later_complete_reopen_CAS_and_no_business_side_effects")
    unsupported_checks = unsupported_http(admin)
    passed.append("generic_unknown_saved_apply_restart_and_queued_dispatch_rejections")
    oc_checks, oc_context = gpu_oc_failure_http(admin)
    passed.append("GPU_OC_R2_fault_hold_preserves_saved_intent_new_save_and_precise_047_legacy_binding")
    ack_checks = workload_counter_ack_http(admin)
    passed.append("workload_snapshot_complete_ACK_repeat_conflict_retired_epoch_and_stale_heartbeat")
    lifecycle_checks = agent_identity_lifecycle_http(admin)
    passed.append("agent_identity_lifecycle_disable_replacement_delete_guards")
    retired_checks = retired_surface_http(admin)
    passed.append("retired_gpu_profile_and_operations_history_routes_answer_404")
    assets, static_checks = static_http(admin)
    passed.append("actual_static_HTTP_manifest_cache_and_approved_new_UI_bytes")
    stop_service(process)
    reopened_process = start_service("reopened")
    restart_checks = persisted_restart_http(old_session, other, deferred, after_user)
    passed.append("same_DB_reopen_bootstrap_env_password_SV_and_deferred_setup_persistence")
    oc_restart_checks = gpu_oc_persisted_restart_http(oc_context)
    passed.append("GPU_OC_fault_hold_survives_actual_Controller_restart_without_heartbeat_replay")
    stop_service(reopened_process)
    prior_users = prepare_old_schema_clone()
    start_service("legacy")
    upgrade_checks = old_schema_upgrade_http(prior_users)
    passed.append("old_users_schema_actual_ALTER_preserves_credentials_legacy_setup_GET_no_write")
    source_verified(manifest)
    stable = True
except BaseException as exc:
    failure = {"error": type(exc).__name__ + ": " + str(exc)[:500], "traceback": traceback.format_exc(limit=12)}
finally:
    for _label, child, log in reversed(processes):
        stop_service(child)
        log.close()
# Preserve the existing export contract while retaining per-startup logs.
# These contain only this script's private synthetic service activity.
with (OUTPUT / "uvicorn.log").open("xb") as combined:
    for label in ("fresh", "reopened", "legacy"):
        log_path = OUTPUT / ("uvicorn-" + label + ".log")
        if log_path.is_file():
            combined.write(("--- " + label + " ---\n").encode())
            combined.write(log_path.read_bytes())
            combined.write(b"\n")
stopped = bool(processes) and all(child.poll() is not None for _label, child, _log in processes)
report = {"schema": "rigpilot.controller.target-native-smoke.v1",
    "status": "passed" if failure is None and stable and stopped else "failed",
    "scope": "Actual packaged Controller HTTP/SQLite for accounts/setup, legacy upgrade, GPU OC fault holds, unknown miner guards, workload ACKs, agent identity lifecycle, and static asset provenance",
    "selection": "Reusable scoped HTTP scenarios; this report makes no mining, physical AMD64, browser-layout or deterministic thread-interleaving claim",
    "platform": args.environment_note, "python": platform.python_version(), "glibc": platform.libc_ver()[1],
    "controller_version": VERSION, "cache_version": CACHE, "manifest_sha256": args.manifest_sha256,
    "main_sha256": args.main_sha256, "files_verified": len(manifest["files"]), "runtime_wheels_verified": len(wheels),
    "runtime_lock_sha256": sha(lock), "source_inputs_stable": stable,
    "password_and_fresh_setup_HTTP": account_checks, "unsupported_miner_HTTP": unsupported_checks,
    "workload_counter_ack_HTTP": ack_checks,
    "agent_identity_lifecycle_HTTP": lifecycle_checks,
    "retired_surface_HTTP": retired_checks,
    "gpu_oc_failure_hold_HTTP": oc_checks, "gpu_oc_hold_restart_HTTP": oc_restart_checks,
    "restart_persistence_HTTP": restart_checks, "restart_and_legacy_setup_HTTP": upgrade_checks,
    "static_contract_HTTP": static_checks,
    "checks_passed": passed, "static_assets": assets, "smoke_script_sha256": sha(Path(__file__)),
    "network": "Docker none; internal loopback only", "uvicorn_stopped": stopped,
    "service_processes": [{"label": label, "stopped": child.poll() is not None} for label, child, _log in processes],
    "production_accessed": False, "real_devices_accessed": False,
    "miner_or_Agent_binary_executed": False, "failure": failure}
with (OUTPUT / "report.json").open("x") as stream:
    stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
print(json.dumps({"status": report["status"], "checks": len(passed), "report": "/output/report.json"}))
sys.exit(0 if report["status"] == "passed" else 1)
