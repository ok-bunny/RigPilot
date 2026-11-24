"""Small wall-clock deadline watchdog for non-cancellable operations.

``requests`` timeouts bound connect/read inactivity, not the total wall-clock
duration of a response.  A peer which keeps producing a byte before every read
timeout can therefore keep a control-plane request alive forever.  This module
provides a deliberately generic watchdog which can supervise just that narrow
critical section without treating long-running miner or update work as a
heartbeat failure.

The watchdog does not terminate the process on its own.  Production code must
provide the expiry callback explicitly, which keeps platform and service-manager
policy out of this reusable synchronization primitive.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ExpiredDeadline:
    """Description of one operation whose wall-clock deadline elapsed."""

    token: int
    phase: str
    timeout_seconds: float
    elapsed_seconds: float


@dataclass(frozen=True)
class _Lease:
    token: int
    phase: str
    started_at: float
    deadline: float
    timeout_seconds: float


class DeadlineWatchdog:
    """Invoke a callback when an explicitly armed wall-clock deadline expires.

    Multiple operations may be supervised concurrently.  ``disarm`` is tied to
    an opaque monotonically increasing token, so a late completion from an old
    operation cannot accidentally cancel a newer operation's lease.
    """

    def __init__(
        self,
        on_expired: Callable[[ExpiredDeadline], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        thread_name: str = "rigpilot-deadline-watchdog",
    ) -> None:
        if not callable(on_expired):
            raise TypeError("on_expired must be callable")
        self._on_expired = on_expired
        self._clock = clock
        self._condition = threading.Condition()
        self._leases: dict[int, _Lease] = {}
        self._next_token = 1
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name=thread_name,
            daemon=True,
        )
        self._thread.start()

    def arm(self, phase: str, timeout_seconds: float) -> int:
        """Arm one deadline and return the token required to disarm it."""

        if not isinstance(phase, str) or not phase.strip():
            raise ValueError("phase must be a non-empty string")
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be a number")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        started_at = self._clock()
        with self._condition:
            if self._closed:
                raise RuntimeError("watchdog is closed")
            token = self._next_token
            self._next_token += 1
            self._leases[token] = _Lease(
                token=token,
                phase=phase.strip(),
                started_at=started_at,
                deadline=started_at + timeout,
                timeout_seconds=timeout,
            )
            self._condition.notify()
            return token

    def disarm(self, token: int) -> bool:
        """Disarm ``token``; return false if it expired or was already removed."""

        with self._condition:
            removed = self._leases.pop(token, None) is not None
            if removed:
                self._condition.notify()
            return removed

    def close(self, timeout: float = 1.0) -> None:
        """Stop the watcher and discard outstanding leases without firing them."""

        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._leases.clear()
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=max(0.0, float(timeout)))

    def __enter__(self) -> "DeadlineWatchdog":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _run(self) -> None:
        while True:
            expired: list[_Lease] = []
            with self._condition:
                while not self._closed:
                    if not self._leases:
                        self._condition.wait()
                        continue
                    now = self._clock()
                    nearest = min(lease.deadline for lease in self._leases.values())
                    remaining = nearest - now
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    expired_tokens = [
                        token
                        for token, lease in self._leases.items()
                        if lease.deadline <= now
                    ]
                    expired = [self._leases.pop(token) for token in expired_tokens]
                    break
                if self._closed:
                    return
            for lease in expired:
                elapsed = max(0.0, self._clock() - lease.started_at)
                try:
                    self._on_expired(
                        ExpiredDeadline(
                            token=lease.token,
                            phase=lease.phase,
                            timeout_seconds=lease.timeout_seconds,
                            elapsed_seconds=elapsed,
                        )
                    )
                except Exception:
                    # A diagnostic callback must not kill the watcher thread.
                    # Production's fail-stop callback normally never returns.
                    continue

