from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent.logging_utils import _stop_logging_for_tests, configure_logging


def test_configured_file_logging_writes_json(tmp_path: Path):
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    log_file = tmp_path / "logs" / "agent.jsonl"
    try:
        configure_logging("INFO", log_file)
        logging.getLogger("agent.test").warning(
            "installer log test", extra={"event": "test_event"}
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not log_file.exists():
            time.sleep(0.01)
        while time.monotonic() < deadline and not log_file.read_text(
            encoding="utf-8"
        ).strip():
            time.sleep(0.01)
        record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
        assert record["message"] == "installer log test"
        assert record["event"] == "test_event"
    finally:
        _stop_logging_for_tests()
        for handler in root.handlers:
            if handler not in previous_handlers:
                handler.close()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)


def test_logging_emit_does_not_wait_for_a_blocked_destination(monkeypatch):
    import threading

    import agent.logging_utils as logging_utils

    entered = threading.Event()
    release = threading.Event()

    class BlockingStream(logging.Handler):
        def emit(self, _record):
            entered.set()
            release.wait(5)

    monkeypatch.setattr(logging_utils.logging, "StreamHandler", BlockingStream)
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        configure_logging("INFO")
        before = time.monotonic()
        logging.getLogger("agent.test").warning("slow disk simulation")
        elapsed = time.monotonic() - before
        assert elapsed < 0.1
        assert entered.wait(1)
        before = time.monotonic()
        logging.getLogger("agent.test").warning("heartbeat still progresses")
        assert time.monotonic() - before < 0.1
    finally:
        release.set()
        _stop_logging_for_tests()
        root.handlers[:] = previous_handlers
        root.setLevel(previous_level)
