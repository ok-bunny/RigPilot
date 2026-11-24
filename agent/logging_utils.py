"""JSON logging with predictable fields for service collectors."""

from __future__ import annotations

import json
import logging
import queue
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
_LOG_QUEUE_RECORDS = 2048


class _DroppingQueueHandler(QueueHandler):
    """Never let a slow journal or failing data disk park the Agent loop.

    The miner is an independent process, so a synchronous RotatingFileHandler
    can create the exact misleading state where mining continues but the Agent
    stops heartbeating.  A bounded queue keeps logging best-effort under disk
    failure without replacing that outage with unbounded memory growth.
    """

    def __init__(self, records: "queue.Queue[logging.LogRecord]") -> None:
        super().__init__(records)
        self.dropped_records = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            # Do not call handleError(): it writes synchronously to stderr and
            # can block on the same journald/storage failure we are isolating.
            self.dropped_records += 1


# Keep strong references for the process lifetime.  QueueListener's worker is
# a daemon thread; a storage device stuck in uninterruptible I/O therefore does
# not block the heartbeat thread or normal process fail-stop handling.
_ACTIVE_LISTENERS: list[QueueListener] = []


def _stop_logging_for_tests() -> None:
    """Drain and close listeners in unit tests; production relies on OS close."""

    while _ACTIVE_LISTENERS:
        listener = _ACTIVE_LISTENERS.pop()
        listener.stop()
        for handler in listener.handlers:
            handler.close()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        output: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and not key.startswith("_"):
                output[key] = value
        if record.exc_info:
            output["exception"] = self.formatException(record.exc_info)
        return json.dumps(output, default=str, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str, log_file: Path | None = None) -> None:
    formatter = JsonFormatter()
    destinations: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            destinations.append(
                RotatingFileHandler(
                    log_file,
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            raise RuntimeError(f"could not open Agent log file {log_file}") from exc
    for handler in destinations:
        handler.setFormatter(formatter)
    records: queue.Queue[logging.LogRecord] = queue.Queue(
        maxsize=_LOG_QUEUE_RECORDS
    )
    non_blocking = _DroppingQueueHandler(records)
    listener = QueueListener(records, *destinations, respect_handler_level=True)
    listener.start()
    _ACTIVE_LISTENERS.append(listener)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(non_blocking)
    root.setLevel(level)
