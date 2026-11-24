"""Isolated regressions for current Controller review fixes, including baseline replay."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

import server
if os.getenv("RIGPILOT_REVIEW28_MAIN_BASELINE"):
    baseline = Path(os.environ["RIGPILOT_REVIEW28_MAIN_BASELINE"])
    spec = importlib.util.spec_from_file_location("server.review28_baseline_main", baseline)
    baseline_main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline_main)
    server.main = baseline_main
    sys.modules["server.main"] = baseline_main

from server import main as main_module
from server.config import Settings
from test_device_gpu_oc import auth as device_auth, login, enroll, gpu_heartbeat


@pytest.fixture()
def review_client(tmp_path, monkeypatch):
    catalog_type = main_module.ManagedMinerCatalog
    catalog = catalog_type(fetcher=lambda *_: [])
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog)
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "review.db", admin_username="admin",
        secret_key="review28-isolated-controller-secret-key-long-enough",
        admin_password="correct-horse-battery-staple")
    with TestClient(main_module.create_app(settings)) as value:
        yield value


@pytest.mark.parametrize("gpus", [{"unexpected":"shape"}, "unexpected-shape"])
def test_malformed_gpu_collection_cannot_delete_saved_state_or_queue_reset(review_client, gpus):
    c = review_client
    token = login(c)
    agent_id, secret = enroll(c, token)
    assert gpu_heartbeat(c,agent_id,secret).status_code == 200
    desired = [{"gpu_uuid":"GPU-5080-stable-uuid","name":"saved",
        "setting":{"index":2,"power_limit_w":250}}]
    with c.app.state.db.connection() as db:
        db.execute("UPDATE agents SET desired_gpu_oc_json=?, metrics_json=? WHERE id=?",
            (json.dumps(desired),json.dumps({"gpus":gpus}),agent_id))
    response = c.delete(f"/api/agents/{agent_id}/gpu-oc/2?gpu_uuid=GPU-5080-stable-uuid&forget_stale=true",headers=device_auth(token))
    assert response.status_code == 409
    with c.app.state.db.connection() as db:
        assert json.loads(db.execute("SELECT desired_gpu_oc_json FROM agents WHERE id=?",(agent_id,)).fetchone()[0]) == desired
        assert db.execute("SELECT COUNT(*) FROM commands WHERE agent_id=?",(agent_id,)).fetchone()[0] == 0
