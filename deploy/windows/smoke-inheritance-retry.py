#!/usr/bin/env python3
"""Exercise response-loss device inheritance with the frozen Windows Agent.

The controller is a temporary loopback-only HTTP server. No real Controller,
device, production token, or network interface is used.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
from typing import Any

REPLACEMENT_TOKEN = "rpr_" + "R" * 43
OTHER_TOKEN = "rpr_" + "X" * 43
AGENT_ID = "11111111-2222-4333-8444-555555555555"
SECRET_PATTERN = re.compile(r"rpa_[A-Za-z0-9_-]{48}\Z")
REQUEST_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


class LocalController(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RequestHandler)
        self.claims: list[dict[str, str]] = []
        self.heartbeats = 0
        self.error: str | None = None


class RequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: LocalController

    def log_message(self, *_args: object) -> None:
        pass

    def respond(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 64 * 1024:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("request body is not an object")
            if self.path == "/api/agent/replace":
                token = body.get("replacement_token")
                request_id = body.get("request_id")
                secret = body.get("proposed_agent_secret")
                if (token != REPLACEMENT_TOKEN
                        or not isinstance(request_id, str)
                        or REQUEST_PATTERN.fullmatch(request_id) is None
                        or not isinstance(secret, str)
                        or SECRET_PATTERN.fullmatch(secret) is None):
                    raise ValueError("invalid synthetic inheritance claim")
                claim = {"request_id": request_id, "secret": secret}
                if self.server.claims and claim != self.server.claims[0]:
                    raise ValueError("inheritance retry changed request material")
                self.server.claims.append(claim)
                if len(self.server.claims) == 1:
                    # The claim was accepted, but the Agent sees no response.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.respond(201, {
                    "agent_id": AGENT_ID,
                    "heartbeat_interval": 10,
                    "inherited_existing_device": True,
                })
                return
            if self.path == "/api/agent/heartbeat":
                if (not self.server.claims
                        or self.headers.get("X-Agent-ID") != AGENT_ID
                        or self.headers.get("Authorization")
                        != "Bearer " + self.server.claims[0]["secret"]):
                    raise ValueError("heartbeat identity mismatch")
                self.server.heartbeats += 1
                self.respond(200, {"commands": []})
                return
            raise ValueError("unexpected request endpoint")
        except (OSError, ValueError, KeyError) as exc:
            self.server.error = type(exc).__name__
            try:
                self.respond(400, {"detail": "synthetic request rejected"})
            except OSError:
                pass


def invoke(agent: Path, config: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [str(agent), "--config", str(config), "--once"],
        cwd=str(agent.parent),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=flags,
    )
    if any(value in result.stdout + result.stderr for value in (REPLACEMENT_TOKEN, OTHER_TOKEN)):
        raise RuntimeError("a one-time code reached Agent output")
    return result


def run(agent: Path, work_dir: Path, version: str) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("run this native smoke only on Windows")
    if not agent.is_file() or work_dir.exists() or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("unsafe frozen Agent or smoke work directory")
    version_result = subprocess.run(
        [str(agent), "--version"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if version_result.returncode != 0 or version_result.stdout.strip() != f"RigPilot Agent {version}":
        raise RuntimeError("frozen Agent version does not match the smoke request")

    work_dir.mkdir(mode=0o700)
    config = work_dir / "config.json"
    state = work_dir / "state.json"
    pending = work_dir / ".state.json.replacement-pending"
    log = work_dir / "agent.jsonl"
    controller = LocalController()
    thread = threading.Thread(target=controller.serve_forever, daemon=True)
    thread.start()
    try:
        config.write_text(json.dumps({
            "server_url": f"http://127.0.0.1:{controller.server_port}",
            "state_file": str(state),
            "name": "windows-inheritance-retry-smoke",
            "log_file": str(log),
            "collect_gpu_metrics": False,
            "managed_mining_enabled": False,
            "allow_remote_sessions": False,
            "allow_agent_updates": False,
        }), encoding="utf-8")
        env = os.environ.copy()
        for key in (
            "RIGPILOT_ENROLLMENT_TOKEN", "RIGPILOT_REPLACEMENT_TOKEN",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        ):
            env.pop(key, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["RIGPILOT_REPLACEMENT_TOKEN"] = REPLACEMENT_TOKEN
        first = invoke(agent, config, env)
        if first.returncode == 0 or state.exists() or not pending.is_file() or len(controller.claims) != 1:
            raise RuntimeError("lost response did not retain one pending inheritance request")
        env["RIGPILOT_REPLACEMENT_TOKEN"] = OTHER_TOKEN
        wrong = invoke(agent, config, env)
        if wrong.returncode == 0 or state.exists() or len(controller.claims) != 1:
            raise RuntimeError("different inheritance code was not rejected locally")
        env["RIGPILOT_REPLACEMENT_TOKEN"] = REPLACEMENT_TOKEN
        second = invoke(agent, config, env)
        if second.returncode != 0 or len(controller.claims) != 2 or controller.claims[0] != controller.claims[1]:
            raise RuntimeError("original inheritance code did not replay identical request material")
        if not state.is_file() or pending.exists() or controller.heartbeats != 1:
            raise RuntimeError("inherited state or authenticated heartbeat did not finalize")
        saved = json.loads(state.read_text(encoding="utf-8-sig"))
        if (saved.get("agent_id") != AGENT_ID
                or saved.get("agent_secret") != controller.claims[0]["secret"]):
            raise RuntimeError("inherited Agent ID or proposed secret was not preserved")
        env.pop("RIGPILOT_REPLACEMENT_TOKEN", None)
        third = invoke(agent, config, env)
        if third.returncode != 0 or len(controller.claims) != 2 or controller.heartbeats != 2:
            raise RuntimeError("saved identity did not heartbeat without another claim")
        for attempt in (first, wrong, second, third):
            if controller.claims[0]["secret"] in attempt.stdout + attempt.stderr:
                raise RuntimeError("Agent output contains the inherited secret")
        if log.exists():
            content = log.read_text(encoding="utf-8")
            if any(value in content for value in (
                REPLACEMENT_TOKEN, OTHER_TOKEN, saved["agent_secret"],
            )):
                raise RuntimeError("Agent log contains inheritance credentials")
        if controller.error:
            raise RuntimeError("synthetic Controller observed an invalid request")
        return {
            "passed": True,
            "frozen_version": version,
            "claim_attempts": len(controller.claims),
            "same_request_and_secret_on_retry": True,
            "different_code_rejected_before_post": True,
            "authenticated_heartbeats": controller.heartbeats,
            "existing_state_no_new_claim": True,
            "server_interface": "127.0.0.1",
            "no_real_controller_or_device": True,
        }
    finally:
        controller.shutdown()
        controller.server_close()
        thread.join(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists() or not args.report.parent.is_dir():
        raise SystemExit("refusing to replace or misplace a smoke report")
    try:
        result = run(args.agent, args.work_dir, args.expected_version)
    except Exception as exc:
        print(f"frozen inheritance retry smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    with args.report.open("x", encoding="utf-8") as output:
        json.dump(result, output, sort_keys=True)
        output.write("\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
