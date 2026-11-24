"""Synthetic Controller→Agent power validation; never load a real NVML library."""
from dataclasses import replace
import json
import re
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agent.config import AgentConfig
from agent.gpu_oc import GpuOcError, NvmlGpuOcBackend
from agent.mining_commands import MiningCommandError, validate_gpu_profile
from server.main import _desired_gpu_oc_entries
from server.schemas import AgentGpuOcBatchSetting, FlightGpuSetting, GpuSetting
from test_review28_controller_cleanup import review_client
from test_device_gpu_oc import auth, enroll, gpu_inventory_heartbeat, login

MODELS = [(GpuSetting, {"index": 2}), (FlightGpuSetting, {"model_match": "Test GPU"}),
          (AgentGpuOcBatchSetting, {})]


@pytest.mark.parametrize("model,identity", MODELS)
@pytest.mark.parametrize("power", [15, 1200])
def test_request_models_allow_positive_finite_power_without_a_global_watt_range(model, identity, power):
    assert model.model_validate({**identity, "power_limit_w": power}).power_limit_w == power


@pytest.mark.parametrize("model,identity", MODELS)
@pytest.mark.parametrize("power", [0, -1, True, False, float("nan"), float("inf"), float("-inf"), "15"])
def test_request_models_reject_nonpositive_nonfinite_boolean_and_string_power(model, identity, power):
    with pytest.raises(ValidationError):
        model.model_validate({**identity, "power_limit_w": power})


@pytest.mark.parametrize("power", [15, 1200])
def test_positive_power_history_is_read_without_losing_gpu_identity(power):
    desired = [{"gpu_uuid": "GPU-power-synthetic", "name": "saved", "setting": {"index": 2, "power_limit_w": power}}]
    assert _desired_gpu_oc_entries(json.dumps(desired)) == desired


def test_unrepresentable_stored_numeric_power_is_rejected_without_an_overflow():
    desired = [{"gpu_uuid": "GPU-power-synthetic", "name": "saved", "setting": {"index": 2, "power_limit_w": 10**400}}]
    assert _desired_gpu_oc_entries(json.dumps(desired)) == []


def reported_gpu(minimum, maximum):
    return {"index": 2, "name": "Synthetic GPU", "vendor": "nvidia", "uuid": "GPU-power-synthetic",
            "oc_capabilities": ["power_limit_w"], "power_min_limit_w": minimum, "power_max_limit_w": maximum}


def submit(client, token, agent_id, mode, power, gpu_uuid="GPU-power-synthetic"):
    if mode == "single":
        path = f"/api/agents/{agent_id}/gpu-oc/apply"
        body = {"vendor": "nvidia", "gpu_uuid": gpu_uuid, "setting": {"index": 2, "power_limit_w": power}}
    else:
        path = f"/api/agents/{agent_id}/gpu-oc/batch-apply"
        body = {"vendor": "nvidia", "gpu_uuids": [gpu_uuid], "setting": {"power_limit_w": power}}
    return client.post(path, headers=auth(token), json=body)


@pytest.mark.parametrize("mode", ["single", "batch"])
@pytest.mark.parametrize("power", [15, 1200])
def test_device_api_accepts_reported_hardware_power_and_preserves_saved_state(review_client, mode, power):
    client = review_client
    token = login(client)
    agent_id, secret = enroll(client, token, version="0.8.25")
    assert gpu_inventory_heartbeat(client, agent_id, secret, [reported_gpu(5, 1500)], version="0.8.25").status_code == 200
    response = submit(client, token, agent_id, mode, power)
    assert response.status_code in {200, 201}, response.text
    detail = client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()
    assert len(detail["desired_gpu_oc"]) == 1
    intent_id = detail["desired_gpu_oc"][0]["intent_id"]
    assert isinstance(intent_id, str) and re.fullmatch(r"[0-9a-f]{32}", intent_id)
    assert detail["desired_gpu_oc"] == [{"gpu_uuid": "GPU-power-synthetic", "name": "Synthetic GPU · GPU 2",
                                           "setting": {"index": 2, "power_limit_w": float(power)},
                                           "intent_id": intent_id}]
    assert client.get(f"/api/agents/{agent_id}", headers=auth(token)).json()["desired_gpu_oc"] == detail["desired_gpu_oc"]


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_device_api_still_rejects_reported_hardware_range_and_changed_uuid(review_client, mode):
    client = review_client
    token = login(client)
    agent_id, secret = enroll(client, token, version="0.8.25")
    assert gpu_inventory_heartbeat(client, agent_id, secret, [reported_gpu(50, 300)], version="0.8.25").status_code == 200
    response = submit(client, token, agent_id, mode, 1200)
    assert response.status_code == 409
    assert "hardware range" in response.text
    response = submit(client, token, agent_id, mode, 100, gpu_uuid="GPU-wrong-synthetic")
    assert response.status_code == 409
    with client.app.state.db.connection() as db:
        assert db.execute("SELECT desired_gpu_oc_json FROM agents WHERE id=?", (agent_id,)).fetchone()[0] == "[]"
        assert db.execute("SELECT COUNT(*) FROM commands WHERE agent_id=?", (agent_id,)).fetchone()[0] == 0


def fake_nvml(minimum, maximum, uuid="GPU-power-synthetic"):
    writes = []
    def handle(index, output):
        output._obj.value = 1000 + index.value
        return 0
    def constraints(_handle, low, high):
        low._obj.value, high._obj.value = minimum * 1000, maximum * 1000
        return 0
    def identity(_handle, output, _length):
        output.value = uuid.encode()
        return 0
    def set_power(_handle, power):
        writes.append(power.value)
        return 0
    library = SimpleNamespace(nvmlInit_v2=lambda: 0, nvmlShutdown=lambda: 0,
        nvmlDeviceGetHandleByIndex_v2=handle, nvmlDeviceGetUUID=identity,
        nvmlDeviceGetPowerManagementLimitConstraints=constraints, nvmlDeviceSetPowerManagementLimit=set_power)
    return library, writes


@pytest.mark.parametrize("power,minimum,maximum", [(15, 5, 25), (1200, 50, 1500)])
def test_agent_still_enforces_explicit_policy_nvml_bounds_and_uuid_before_mock_writes(tmp_path, power, minimum, maximum):
    config = AgentConfig(server_url="https://synthetic.example", state_file=tmp_path / "state.json", allow_gpu_oc=True)
    payload = {"gpu_profile_id": "power-synthetic", "name": "test", "vendor": "nvidia",
               "settings": [{"index": 2, "power_limit_w": power}]}
    _, settings = validate_gpu_profile(payload, config)
    library, writes = fake_nvml(minimum, maximum)
    backend = NvmlGpuOcBackend(library)
    bound = [{**settings[0], "_expected_gpu_uuid": "GPU-power-synthetic"}]
    assert backend.apply(bound)["operations"] == [{"index": 2, "applied": ["power_limit_w"]}]
    assert writes == [power * 1000]
    with pytest.raises(MiningCommandError, match="local range"):
        validate_gpu_profile(payload, replace(config, gpu_power_limit_min_w=50, gpu_power_limit_max_w=300))
    for hardware_low, hardware_high, uuid in [(50, 300, "GPU-power-synthetic"), (minimum, maximum, "GPU-changed")]:
        library, rejected_writes = fake_nvml(hardware_low, hardware_high, uuid)
        with pytest.raises(GpuOcError):
            NvmlGpuOcBackend(library).apply(bound)
        assert rejected_writes == []
