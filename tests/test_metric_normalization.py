from server.main import _normalize_metrics


def test_detailed_agent_metrics_have_dashboard_summary_fields():
    normalized = _normalize_metrics(
        {
            "cpu": {"percent": 37.5},
            "memory": {"percent": 62.0},
            "disks": [{"percent": 51.0}, {"percent": 83.0}],
            "gpus": [
                {
                    "name": "Example GPU",
                    "memory_total_mib": 10_000,
                    "memory_used_mib": 2_500,
                }
            ],
        }
    )

    assert normalized["cpu_percent"] == 37.5
    assert normalized["memory_percent"] == 62.0
    assert normalized["disk_percent"] == 83.0
    assert normalized["gpus"][0]["memory_percent"] == 25.0
