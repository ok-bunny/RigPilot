from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from server import __version__
from server import system_metrics
from server.config import Settings
from server.main import create_app
from server.system_metrics import ControllerMetricsSampler


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def write_proc(proc_root: Path, *, cpu: str, receive: int, send: int) -> None:
    (proc_root / "net").mkdir(parents=True, exist_ok=True)
    (proc_root / "stat").write_text(cpu + "\n", encoding="utf-8")
    (proc_root / "meminfo").write_text(
        "\n".join(
            (
                "MemTotal:       1000000 kB",
                "MemAvailable:    400000 kB",
                "SwapTotal:       200000 kB",
                "SwapFree:        150000 kB",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (proc_root / "net" / "dev").write_text(
        "Inter-| Receive | Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        f"  lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n"
        f"eth0: {receive} 0 0 0 0 0 0 0 {send} 0 0 0 0 0 0 0\n",
        encoding="utf-8",
    )


def test_controller_sampler_reports_usage_and_real_network_rates(tmp_path: Path):
    proc_root = tmp_path / "proc"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    clock = FakeClock()
    write_proc(
        proc_root,
        cpu="cpu 100 0 50 850 0 0 0 0 50 0",
        receive=1_000,
        send=2_000,
    )
    sampler = ControllerMetricsSampler(
        data_dir, proc_root=proc_root, clock=clock
    )

    first = sampler.sample()
    assert first["cpu_percent"] is None
    assert first["network"]["receive_bytes_per_second"] is None
    assert first["memory"]["percent"] == 60.0
    assert first["swap"]["percent"] == 25.0
    assert 0 <= first["disk"]["percent"] <= 100

    clock.value += 10
    write_proc(
        proc_root,
        cpu="cpu 200 0 100 1700 0 0 0 0 100 0",
        receive=6_000,
        send=5_000,
    )
    second = sampler.sample()

    assert second["cpu_percent"] == 15.0
    assert second["network"] == {
        "receive_bytes_per_second": 500.0,
        "send_bytes_per_second": 300.0,
        "sample_seconds": 10.0,
    }


def test_network_rates_rebaseline_after_interface_changes_and_counter_resets(tmp_path):
    proc = tmp_path / "proc"
    clock = FakeClock()
    sampler = ControllerMetricsSampler(tmp_path, proc_root=proc, clock=clock)

    def sample(rx, tx, extra=None):
        clock.value += 10
        write_proc(proc, cpu="cpu 10 0 0 20", receive=rx, send=tx)
        if extra is not None:
            with (proc / "net/dev").open("a") as f:
                f.write(f"vpn0: {extra} 0 0 0 0 0 0 0 {extra} 0 0 0 0 0 0 0\n")
        return sampler.sample()["network"]["receive_bytes_per_second"]

    assert sample(1000, 2000) is None
    assert sample(1500, 2500, 10_000) is None
    assert sample(1600, 2600, 10_100) == 20
    assert sample(5, 10, 30_100) is None
    assert sample(105, 110, 30_200) == 20
    assert sample(40_000, 50_000) is None
    assert sample(40_100, 50_100) == 10


def test_controller_sampler_fails_fields_closed_when_procfs_is_missing(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sampler = ControllerMetricsSampler(data_dir, proc_root=tmp_path / "missing")

    result = sampler.sample()

    assert result["cpu_percent"] is None
    assert result["memory"] is None
    assert result["swap"] is None
    assert result["network"]["receive_bytes_per_second"] is None
    assert result["disk"]["total_bytes"] > 0


@pytest.fixture
def mac_metrics(monkeypatch):
    state = SimpleNamespace(
        cpu=SimpleNamespace(user=100.1, nice=5.1, system=50.1, idle=844.7),
        memory=SimpleNamespace(total=1000, available=400, used=750),
        swap=SimpleNamespace(total=200, free=150),
        network={
            "lo0": SimpleNamespace(bytes_recv=999_000, bytes_sent=999_000),
            "en0": SimpleNamespace(bytes_recv=1000, bytes_sent=2000),
            "en1": SimpleNamespace(bytes_recv=500, bytes_sent=300),
        },
    )

    def net_io_counters(*, pernic, nowrap):
        assert pernic is True
        assert nowrap is False
        return state.network

    backend = SimpleNamespace(
        Error=RuntimeError,
        cpu_times=lambda: state.cpu,
        virtual_memory=lambda: state.memory,
        swap_memory=lambda: state.swap,
        net_io_counters=net_io_counters,
    )
    monkeypatch.setattr(system_metrics, "platform_system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "psutil", backend)
    return state, backend


def test_mac_sampler_uses_available_memory_and_excludes_loopback(
    tmp_path: Path, mac_metrics
):
    state, _ = mac_metrics
    clock = FakeClock()
    sampler = ControllerMetricsSampler(tmp_path, clock=clock)

    first = sampler.sample()
    assert first["cpu_percent"] is None
    assert first["network"]["receive_bytes_per_second"] is None
    assert first["memory"] == {
        "total_bytes": 1000,
        "used_bytes": 600,
        "available_bytes": 400,
        "percent": 60.0,
    }
    assert first["swap"] == {
        "total_bytes": 200,
        "used_bytes": 50,
        "free_bytes": 150,
        "percent": 25.0,
    }

    clock.value += 10
    state.cpu = SimpleNamespace(user=100.2, nice=5.2, system=50.2, idle=845.4)
    state.network["lo0"] = SimpleNamespace(bytes_recv=1_999_000, bytes_sent=1_999_000)
    state.network["en0"] = SimpleNamespace(bytes_recv=5000, bytes_sent=4000)
    state.network["en1"] = SimpleNamespace(bytes_recv=1500, bytes_sent=1300)
    result = sampler.sample()

    assert result["cpu_percent"] == 30.0
    assert result["network"] == {
        "receive_bytes_per_second": 500.0,
        "send_bytes_per_second": 300.0,
        "sample_seconds": 10.0,
    }


@pytest.mark.parametrize(
    ("source", "field"),
    [
        ("cpu_times", "cpu_percent"),
        ("virtual_memory", "memory"),
        ("swap_memory", "swap"),
        ("net_io_counters", "network"),
    ],
)
def test_mac_sampler_isolates_failed_sources(
    tmp_path: Path, mac_metrics, monkeypatch, source, field
):
    state, backend = mac_metrics
    clock = FakeClock()
    sampler = ControllerMetricsSampler(tmp_path, clock=clock)
    sampler.sample()

    def fail(*args, **kwargs):
        raise OSError("metric unavailable")

    monkeypatch.setattr(backend, source, fail)
    clock.value += 1
    state.cpu = SimpleNamespace(user=200.1, nice=5.1, system=50.1, idle=844.7)
    result = sampler.sample()
    for name in ("cpu_percent", "memory", "swap", "network"):
        value = result[name]
        if name == "network":
            value = value["receive_bytes_per_second"]
        assert (value is None) == (name == field)
    assert result["disk"]["total_bytes"] > 0


def test_mac_sampler_handles_counter_reset_and_no_swap(tmp_path: Path, mac_metrics):
    state, _ = mac_metrics
    clock = FakeClock()
    sampler = ControllerMetricsSampler(tmp_path, clock=clock)
    sampler.sample()

    clock.value += 1
    state.cpu = SimpleNamespace(user=0, nice=0, system=0, idle=0)
    state.network = {"en0": SimpleNamespace(bytes_recv=0, bytes_sent=0)}
    state.swap = SimpleNamespace(total=0, free=0)
    result = sampler.sample()
    assert result["cpu_percent"] is None
    assert result["network"]["receive_bytes_per_second"] is None
    assert result["network"]["send_bytes_per_second"] is None
    assert result["swap"]["percent"] == 0.0

    clock.value += 1
    state.cpu = SimpleNamespace(user=0.5, nice=0, system=0, idle=0.5)
    state.network = {"en0": SimpleNamespace(bytes_recv=100, bytes_sent=200)}
    recovered = sampler.sample()
    assert recovered["cpu_percent"] == 50.0
    assert recovered["network"]["receive_bytes_per_second"] == 100.0
    assert recovered["network"]["send_bytes_per_second"] == 200.0


def test_explicit_proc_root_still_uses_linux_metrics_on_mac(tmp_path: Path, mac_metrics):
    proc_root = tmp_path / "proc"
    write_proc(proc_root, cpu="cpu 100 0 50 850", receive=1000, send=2000)
    result = ControllerMetricsSampler(tmp_path, proc_root=proc_root).sample()
    assert result["memory"]["total_bytes"] == 1_000_000 * 1024


def test_authenticated_summary_exposes_controller_version_and_bounded_metrics(
    tmp_path: Path,
):
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.db",
        secret_key="controller-metrics-test-key-that-is-long-enough",
        admin_username="admin",
        admin_password="correct-horse-battery-staple",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/summary").status_code == 401
        login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "correct-horse-battery-staple"},
        )
        token = login.json()["token"]
        response = client.get(
            "/api/summary", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    controller = response.json()["controller"]
    assert controller["version"] == __version__
    assert set(controller) == {
        "version",
        "collected_at",
        "cpu_percent",
        "memory",
        "swap",
        "disk",
        "network",
    }
    assert set(controller["network"]) == {
        "receive_bytes_per_second",
        "send_bytes_per_second",
        "sample_seconds",
    }
