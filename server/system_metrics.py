from __future__ import annotations

import math
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from platform import system as platform_system
from typing import Any, Callable


def _percent(used: int, total: int) -> float | None:
    if total <= 0:
        return None
    value = (max(0, min(used, total)) / total) * 100
    return round(value, 1) if math.isfinite(value) else None


def _byte_usage(total: int, free: int, *, free_key: str) -> dict[str, Any]:
    free = max(0, min(free, total))
    used = max(0, total - free)
    return {
        "total_bytes": total,
        "used_bytes": used,
        free_key: free,
        "percent": _percent(used, total) if total else 0.0,
    }


class ControllerMetricsSampler:
    """Linux/macOS host sampler for the controller dashboard.

    Linux uses procfs; macOS uses psutil. Each source fails independently so a
    missing host metric never breaks the authenticated summary endpoint.
    Network and CPU rates need two samples;
    the first response deliberately reports ``None`` instead of a fake zero.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        proc_root: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data_dir = data_dir
        self._proc_root = proc_root if proc_root is not None else Path("/proc")
        self._psutil: Any = None
        # An explicit proc_root remains usable for Linux fixtures on any host.
        if proc_root is None and platform_system() == "Darwin":
            try:
                import psutil

                self._psutil = psutil
            except ImportError:
                # Keep the dashboard available if the optional backend is absent.
                pass
        self._clock = clock
        self._lock = threading.Lock()
        self._last_sample_time: float | None = None
        self._last_cpu: tuple[float, float] | None = None
        self._last_network: dict[str, tuple[int, int]] | None = None

    def _cpu_counters(self) -> tuple[float, float] | None:
        if self._psutil is not None:
            try:
                times = self._psutil.cpu_times()
                return times.user + times.nice + times.system + times.idle, times.idle
            except (OSError, ValueError, self._psutil.Error):
                return None
        try:
            line = (self._proc_root / "stat").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
            fields = line.split()
            if not fields or fields[0] != "cpu" or len(fields) < 5:
                return None
            values = [int(value) for value in fields[1:11]]
            # Linux guest/guest_nice are already included in user/nice.
            total = sum(values[:8])
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            return total, idle
        except (OSError, ValueError, IndexError):
            return None

    def _memory(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self._psutil is not None:
            return self._mac_memory()
        try:
            values: dict[str, int] = {}
            for line in (self._proc_root / "meminfo").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                key, separator, raw = line.partition(":")
                if not separator:
                    continue
                amount = raw.strip().split()[0]
                values[key] = int(amount) * 1024
            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable")
            if available is None:
                available = sum(
                    values.get(key, 0)
                    for key in ("MemFree", "Buffers", "Cached", "SReclaimable")
                )
            memory = (
                _byte_usage(total, available, free_key="available_bytes")
                if total > 0
                else None
            )
            swap = _byte_usage(
                values.get("SwapTotal", 0), values.get("SwapFree", 0),
                free_key="free_bytes",
            )
            return memory, swap
        except (OSError, ValueError, IndexError):
            return None, None

    def _mac_memory(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        memory = None
        swap = None
        try:
            usage = self._psutil.virtual_memory()
            if usage.total > 0:
                memory = _byte_usage(
                    usage.total, usage.available, free_key="available_bytes"
                )
        except (OSError, ValueError, self._psutil.Error):
            pass
        try:
            usage = self._psutil.swap_memory()
            swap = _byte_usage(usage.total, usage.free, free_key="free_bytes")
        except (OSError, ValueError, self._psutil.Error):
            pass
        return memory, swap

    def _network_counters(self) -> dict[str, tuple[int, int]] | None:
        if self._psutil is not None:
            try:
                # Keep our own reset detection, as for Linux, without a global cache.
                counters = self._psutil.net_io_counters(pernic=True, nowrap=False)
                return {
                    name: (value.bytes_recv, value.bytes_sent)
                    for name, value in counters.items() if name != "lo0"
                } or None
            except (OSError, ValueError, self._psutil.Error):
                return None
        try:
            interfaces = {}
            for line in (self._proc_root / "net" / "dev").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[2:]:
                interface, separator, counters = line.partition(":")
                if not separator or interface.strip() == "lo":
                    continue
                fields = counters.split()
                if len(fields) < 16:
                    continue
                interfaces[interface.strip()] = int(fields[0]), int(fields[8])
            return interfaces or None
        except (OSError, ValueError, IndexError):
            return None

    def _disk(self) -> dict[str, Any] | None:
        try:
            usage = shutil.disk_usage(self._data_dir)
            return {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "percent": _percent(usage.used, usage.total),
            }
        except OSError:
            return None

    def sample(self) -> dict[str, Any]:
        memory, swap = self._memory()
        disk = self._disk()
        cpu_percent: float | None = None
        receive_rate: float | None = None
        send_rate: float | None = None
        sample_seconds: float | None = None
        # Read rate counters and advance their baselines under one lock so
        # concurrent summary requests cannot move the sampler backwards.
        with self._lock:
            now = self._clock()
            current_cpu = self._cpu_counters()
            current_network = self._network_counters()
            if self._last_sample_time is not None:
                elapsed = now - self._last_sample_time
                if elapsed > 0:
                    if current_cpu is not None and self._last_cpu is not None:
                        total_delta = current_cpu[0] - self._last_cpu[0]
                        idle_delta = current_cpu[1] - self._last_cpu[1]
                        if total_delta > 0 and 0 <= idle_delta <= total_delta:
                            cpu_percent = round(
                                max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100)),
                                1,
                            )
                    previous = self._last_network
                    if current_network and previous and current_network.keys() == previous.keys():
                        # A newly visible interface brings its lifetime counters.
                        # A reset on one NIC can also be hidden by traffic on another.
                        deltas = [(rx - previous[n][0], tx - previous[n][1])
                                  for n, (rx, tx) in current_network.items()]
                        if all(rx >= 0 and tx >= 0 for rx, tx in deltas):
                            receive_rate = round(sum(rx for rx, _ in deltas) / elapsed, 1)
                            send_rate = round(sum(tx for _, tx in deltas) / elapsed, 1)
                            sample_seconds = round(elapsed, 3)
            self._last_sample_time = now
            self._last_cpu = current_cpu
            self._last_network = current_network
        return {
            "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cpu_percent": cpu_percent,
            "memory": memory,
            "swap": swap,
            "disk": disk,
            "network": {
                "receive_bytes_per_second": receive_rate,
                "send_bytes_per_second": send_rate,
                "sample_seconds": sample_seconds,
            },
        }
