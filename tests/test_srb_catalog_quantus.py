"""Actual local API/context behavior for official SRB resources and Quantus."""
import hashlib
import pytest
from test_059_backend import app_client, auth, login, create_user
from server.miner_adapters import MINER_ADAPTERS
from server.main import _normalize_managed_options
from server.miner_adapters.srb_catalog import SRB_ALGORITHM_MINIMUM_VERSIONS
from test_independent_dual_mining import enroll_linux_agent

VERSIONS = ["3.4.6","3.5.7","3.5.8","3.6.3","3.6.4","3.6.8","3.6.9","3.7.0","3.7.1","3.7.2"]
@pytest.fixture()
def setup(app_client, monkeypatch):
    client, _ = app_client
    def fake_download(url, destination, **kwargs):
        content = b"offline local SRB context test archive"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return len(content), hashlib.sha256(content).hexdigest()
    monkeypatch.setattr("server.main._download_https_package", fake_download)
    def releases(owner, repo):
        if repo != "SRBMiner-Multi": return []
        return [{"tag_name":v,"draft":False,"prerelease":False,"assets":[{"name":f"SRBMiner-Multi-{v.replace('.', '-')}-{suffix}","browser_download_url":f"https://github.com/{owner}/{repo}/releases/download/{v}/SRBMiner-Multi-{v.replace('.', '-')}-{suffix}","size":1000} for suffix in ["win64.zip","Linux.tar.gz"]]} for v in VERSIONS]
    catalog=client.app.state.managed_miner_catalog;catalog._fetcher=releases;catalog.invalidate();catalog.refresh_sync(force=False)
    admin=login(client)
    def wallet(coin):
        resp=client.post('/api/wallets',headers=auth(admin),json={"name":coin+" payments","coin":coin,"address":"public-payment-address-"+coin.lower()});assert resp.status_code==201,resp.text;return resp.json()['id']
    yield client,admin,wallet

def payload(wallet,version='3.6.9',scheme='stratum+tcp',pin=None,token='pool-tag',mode=None):
    extra={} if pin is None else {"tls_cert_sha256":pin}
    if mode is not None: extra["quic_wallet_mode"]=mode
    return {"name":"Quantus local sheet","miner_type":"srbminer","miner_version":version,"coin":"QUANTUS","wallet_id":wallet,"algorithm":"quantus","pool_url":scheme+"://node.example.test:3333","pool_password":token,"extra_config":extra}

def test_API_delivers_all_current_rows_with_correct_real_resources(setup):
    client, admin, _ = setup
    srb = next(a for a in client.get('/api/miner-adapters', headers=auth(admin)).json()
               if a['miner_type'] == 'srbminer')
    current = [a for a in srb['algorithms'] if a['catalogue_status'] == 'current']
    expected = {a.name: a.public_payload()
                for a in MINER_ADAPTERS.require('srbminer').algorithm_specs
                if a.catalogue_status == 'current'}
    byname = {a['name']: a for a in current}
    assert len(byname) == len(current)  # Each canonical algorithm is delivered once.
    assert byname == expected
    assert byname['cpupower']['supported_resources'] == ['cpu']
    assert set(byname['yespower']['supported_resources']) == {'cpu', 'gpu'}
    assert byname['quantus']['minimum_version'] == '3.6.4'
    assert srb['coin_defaults']['QTC'] == 'qhash'
    assert srb['coin_defaults']['QUANTUS'] == 'quantus'

@pytest.mark.parametrize('version',['3.6.4','3.6.8','3.6.9','3.7.1','3.7.2'])
def test_quantus_lucky_stratum_works_without_QUIC_pin(setup,version):
    client,admin,wallet=setup;b=payload(wallet('QUANTUS'),version);resp=client.post('/api/flight-sheets',headers=auth(admin),json=b);assert resp.status_code==201,resp.text;assert resp.json()['algorithm']=='quantus';assert resp.json()['pool_url']==b['pool_url'];assert 'tls_cert_sha256' not in resp.json()['extra_config']

@pytest.mark.parametrize('pin',['Aa:BB:node-provided-pin','base64+opaque/pin==','f'*64])
def test_quantus_QUIC_preserves_pin_encoding_and_secret_token_with_payment_wallet_separate(setup,pin):
    client,admin,wallet=setup;wid=wallet('QUANTUS');b=payload(wid,scheme='quic',pin=pin,token='private-node-auth-token',mode='token');resp=client.post('/api/flight-sheets',headers=auth(admin),json=b);assert resp.status_code==201,resp.text;assert resp.json()['extra_config']['tls_cert_sha256']==pin
    viewer=create_user(client,admin,'quantus-readonly','viewer');rows=client.get('/api/flight-sheets',headers=auth(viewer)).json();row=next(x for x in rows if x['id']==resp.json()['id']);assert row['wallet_address']=='public-payment-address-quantus';assert row['pool_password'] is None;assert 'private-node-auth-token' not in str(row)
    b['name']='Renamed with exact pin';updated=client.put('/api/flight-sheets/'+resp.json()['id'],headers=auth(admin),json=b);assert updated.status_code==200,updated.text;assert updated.json()['extra_config']['tls_cert_sha256']==pin

@pytest.mark.parametrize('changes,reason', [({'miner_version':'3.6.3'},'3.6.4'),({'extra_config':{}},'tls_cert_sha256'),({'pool_password':''},'authentication token'),({'extra_config':{'tls_cert_sha256':'bad\npin'}},'control characters'),({'extra_config':{'tls_cert_sha256':'x','resource_kind':'cpu'}},'task resource')])
def test_quantus_invalid_real_context_rejected_not_global_latest_gate(setup,changes,reason):
    client,admin,wallet=setup;b=payload(wallet('QUANTUS'),scheme='quic',pin='opaque-node-pin',token='auth-token',mode='token');b.update(changes);resp=client.post('/api/flight-sheets',headers=auth(admin),json=b);assert resp.status_code==422,resp.text;assert reason in resp.text

def test_QUIC_does_not_escape_to_other_SRB_algorithm_or_other_miner(setup):
    client,admin,wallet=setup;wid=wallet('PEARL');b=payload(wid,scheme='quic',pin='pin');b.update(coin='PEARL',algorithm='pearlhash');resp=client.post('/api/flight-sheets',headers=auth(admin),json=b);assert resp.status_code==422 and 'only' in resp.text
    from fastapi import HTTPException
    with pytest.raises(HTTPException):_normalize_managed_options(miner_type='xmrig',coin='XMR',algorithm=None,pool_url='quic://node.test:3333',extra_config={},miner_version='6.26.0')

def test_version_removed_legacy_data_remains_addressable_but_not_claimed_current_binary(setup):
    client,admin,wallet=setup;wid=wallet('WALA');b=payload(wid,version='3.5.7');b.update(coin='WALA',algorithm='walahash');old=client.post('/api/flight-sheets',headers=auth(admin),json=b);assert old.status_code==201,old.text
    b['miner_version']='3.5.8';blocked=client.post('/api/flight-sheets',headers=auth(admin),json=b);assert blocked.status_code==422 and 'removed' in blocked.text
    assert any(x['id']==old.json()['id'] and x['algorithm']=='walahash' and x['miner_version']=='3.5.7' for x in client.get('/api/flight-sheets',headers=auth(admin)).json())

def test_general_CPU_GPU_dual_uses_selected_resource_and_not_randomx_name(setup):
    client,admin,wallet=setup;cpu=wallet('YPC');gpu=wallet('XEL');body={"name":"CPU+GPU official example","miner_type":"srbminer","miner_version":"3.7.1","coin":"YPC","wallet_id":cpu,"algorithm":"yespower","pool_url":"stratum+tcp://cpu.test:3333","extra_config":{"resource_kind":"cpu"},"secondary":{"miner_type":"srbminer","miner_version":"3.7.1","coin":"XEL","wallet_id":gpu,"algorithm":"xelishashv3","pool_url":"stratum+tcp://gpu.test:3333","device_indices":[0,1],"extra_config":{"resource_kind":"gpu"}}};resp=client.post('/api/flight-sheets',headers=auth(admin),json=body);assert resp.status_code==201,resp.text;assert resp.json()['extra_config']['resource_kind']=='cpu';assert resp.json()['secondary']['extra_config']['resource_kind']=='gpu'

@pytest.mark.parametrize('vendor,name,version,valid',[('nvidia','RTX5080','3.6.4',True),('amd','RX580','3.6.4',False),('amd','RX580','3.6.8',True),('intel','ARC A770','3.6.9',False),('intel','ARC A770','3.7.0',True)])
def test_Quantus_hardware_milestones_only_use_official_proven_floor(vendor,name,version,valid):
    adapter=MINER_ADAPTERS.require('srbminer')
    if valid:adapter.validate_hardware_version('quantus',version,vendor=vendor,name=name)
    else:
        with pytest.raises(ValueError):adapter.validate_hardware_version('quantus',version,vendor=vendor,name=name)


@pytest.mark.parametrize("mode", [None, "wallet"])
@pytest.mark.parametrize("password", [None, "", "x"])
def test_public_QUIC_does_not_infer_a_private_node_token(setup, mode, password):
    client, admin, wallet = setup
    body = payload(wallet("QUANTUS"), scheme="quic", pin="node:opaque-pin", mode=mode)
    if password is None:
        body.pop("pool_password")
    else:
        body["pool_password"] = password
    result = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["wallet_address"] == "public-payment-address-quantus"
    assert MINER_ADAPTERS.require("srbminer").quic_wallet_value(data["wallet_address"], data["extra_config"], pool_password=password) == data["wallet_address"]
    if mode is None:
        assert "quic_wallet_mode" not in data["extra_config"]


def test_node_QUIC_requires_explicit_credential_but_does_not_invent_token_syntax(setup):
    client, admin, wallet = setup
    body = payload(wallet("QUANTUS"), scheme="quic", pin="opaque-pin", mode="token")
    body.pop("pool_password")
    missing = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert missing.status_code == 422 and "explicitly supplied" in missing.text
    body["pool_password"] = "x"
    supplied = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert supplied.status_code == 201, supplied.text
    assert MINER_ADAPTERS.require("srbminer").quic_wallet_value("public-payment-wallet", supplied.json()["extra_config"], pool_password="x") == "x"
    body.pop("pool_password")
    missing_update = client.put("/api/flight-sheets/" + supplied.json()["id"], headers=auth(admin), json=body)
    assert missing_update.status_code == 422 and "explicitly supplied" in missing_update.text


@pytest.mark.parametrize("token", [None, "", "   "])
def test_shared_node_QUIC_boundary_rejects_missing_secret(token):
    with pytest.raises(ValueError, match="authentication token"):
        MINER_ADAPTERS.require("srbminer").validate_pool_transport("quic://node.test:3333", "quantus", "3.6.4", {"quic_wallet_mode": "token", "tls_cert_sha256": "pin"}, pool_password=token)


@pytest.mark.parametrize("algorithm,before,first", [("blake3_an", "3.4.6", "3.5.7"), ("noid", "3.5.8", "3.6.3")])
def test_known_algorithm_floor_rejects_older_binary_and_accepts_proven_first(setup, algorithm, before, first):
    client, admin, wallet = setup
    body = payload(wallet("LOCAL"), version=before)
    body.update(coin="LOCAL", algorithm=algorithm)
    too_old = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert too_old.status_code == 422 and first in too_old.text
    body["miner_version"] = first
    compatible = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert compatible.status_code == 201, compatible.text


def test_catalogue_exposes_every_verified_floor_and_keeps_unknown_floors_unknown(setup):
    client, admin, _ = setup
    adapter = next(a for a in client.get("/api/miner-adapters", headers=auth(admin)).json() if a["miner_type"] == "srbminer")
    observed = {a["name"]: a["minimum_version"] for a in adapter["algorithms"] if a["minimum_version"]}
    assert observed == SRB_ALGORITHM_MINIMUM_VERSIONS
    assert MINER_ADAPTERS.require("srbminer").algorithm_spec("yespower").minimum_version is None


def test_resource_override_survives_create_update_and_independent_GPU_OC_apply(setup):
    client, admin, wallet = setup
    body = payload(wallet("YPC"), version="3.7.1")
    body.update(name="Yespower GPU with independent CPU", coin="YPC", algorithm="yespower", device_indices=[0], extra_config={"resource_kind": "gpu"}, secondary_mode="independent", gpu_oc={"vendor": "nvidia", "settings": [{"index": 0, "power_limit_w": 180}]}, secondary={"miner_type": "xmrig", "miner_version": "6.26.0", "coin": "XMR", "wallet_id": wallet("XMR"), "pool_url": "stratum+tcp://cpu.test:3333"})
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    body["name"] += " updated"
    updated = client.put("/api/flight-sheets/" + created.json()["id"], headers=auth(admin), json=body)
    assert updated.status_code == 200, updated.text
    agent_id, _ = enroll_linux_agent(client, admin, version="0.8.24")
    applied = client.post(f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply", headers=auth(admin))
    assert applied.status_code == 201, applied.text
    dispatch = applied.json()["payload"]
    assert dispatch["primary"]["extra_config"]["resource_kind"] == "gpu"
    assert dispatch["primary"]["device_indices"] == [0]
    assert dispatch["gpu_oc"] == updated.json()["gpu_oc"]


@pytest.mark.parametrize("gpus", [None, [{"index": 0, "vendor": "amd", "name": "RX580"}, {"index": 1, "vendor": "nvidia", "name": "RTX5080"}]])
def test_quantus_apply_leaves_binary_device_identity_and_backend_validation_to_agent(setup, gpus):
    client, admin, wallet = setup
    body = payload(wallet("QUANTUS"), version="3.6.4")
    body["device_indices"] = [0]
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    agent_id, secret = enroll_linux_agent(client, admin, version="0.8.24")
    heartbeat = client.post("/api/agent/heartbeat", headers={"X-Agent-ID": agent_id, "Authorization": "Bearer " + secret}, json={"hostname": "quantus-test.local", "os_family": "linux", "platform": "linux", "agent_version": "0.8.24", "capabilities": ["managed_mining.dual_process.v1", "managed_mining.independent_srb_gpu_oc.v1"], "metrics": {"gpus": gpus}, "inventory": {"mining_status": {"gpu_mapping": [{"logical_index": 0, "miner_id": 0, "vendor": "nvidia", "name": "RTX5080", "pci_bus_id": "0000:01:00.0"}]}}})
    assert heartbeat.status_code == 200, heartbeat.text
    applied = client.post(f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply", headers=auth(admin))
    assert applied.status_code == 201, applied.text
    assert applied.json()["payload"]["primary"]["algorithm"] == "quantus"
    assert applied.json()["payload"]["primary"]["device_indices"] == [0]


@pytest.mark.parametrize("mode,password", [(None, "x"), ("wallet", ""), ("token", "node token+opaque/value==")])
def test_QUIC_dispatch_keeps_raw_identity_separate_from_public_wallet(setup, mode, password):
    client, admin, wallet = setup
    body = payload(wallet("QUANTUS"), version="3.6.4", scheme="quic", pin="opaque:certificate-pin", mode=mode, token=password)
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    agent_id, _ = enroll_linux_agent(client, admin, version="0.8.24")
    applied = client.post(f"/api/agents/{agent_id}/flight-sheets/{created.json()['id']}/apply", headers=auth(admin))
    assert applied.status_code == 201, applied.text
    with client.app.state.db.connection() as db:
        row = db.execute("SELECT payload_json FROM commands WHERE id=?", (applied.json()["id"],)).fetchone()
    import json
    primary = json.loads(row["payload_json"])["primary"]
    assert primary["wallet_address"] == "public-payment-address-quantus"
    assert primary["pool_password"] == password
    assert primary["extra_config"]["tls_cert_sha256"] == "opaque:certificate-pin"
    if mode is None:
        assert "quic_wallet_mode" not in primary["extra_config"]
    else:
        assert primary["extra_config"]["quic_wallet_mode"] == mode
    expected = password if mode == "token" else primary["wallet_address"]
    assert MINER_ADAPTERS.require("srbminer").quic_wallet_value(primary["wallet_address"], primary["extra_config"], pool_password=primary["pool_password"]) == expected


@pytest.mark.parametrize("vendor,before,first", [("nvidia", "3.0.6", "3.1.2"), ("amd", "3.1.2", "3.1.4"), ("intel", "3.1.2", "3.1.4")])
def test_xelishashv3_CPU_introduction_does_not_imply_GPU_backend_support(vendor, before, first):
    adapter = MINER_ADAPTERS.require("srbminer")
    assert adapter.validate_algorithm_version("xelishashv3", "3.0.6") == "xelishashv3"
    with pytest.raises(ValueError, match=first):
        adapter.validate_hardware_version("xelishashv3", before, vendor=vendor)
    adapter.validate_hardware_version("xelishashv3", first, vendor=vendor)
    adapter.validate_hardware_version("xelishashv3", "3.0.6", vendor="unknown")


@pytest.mark.parametrize("algorithm", [None, "", "randomx", "rx", "rx/0", " RX/0 "])
def test_srb_XMR_constraint_runs_after_default_and_alias_normalization(algorithm):
    adapter = MINER_ADAPTERS.require("srbminer")
    assert adapter.normalize_algorithm(algorithm, coin=" xmr ") == "randomx"
    with pytest.raises(ValueError, match="XMR"):
        adapter.normalize_algorithm("cpupower", coin="XMR")
    assert adapter.normalize_algorithm("cpupower", coin="CPUCHAIN") == "cpupower"
    # ETC's recommended etchash is not promoted to an exclusive policy.
    assert adapter.normalize_algorithm("cpupower", coin="ETC") == "cpupower"


def test_srb_XMR_non_RandomX_is_rejected_at_create_and_update_boundary(setup):
    client, admin, wallet = setup
    body = payload(wallet("XMR"), version="3.7.1")
    body.update(coin="XMR", algorithm="cpupower", extra_config={"resource_kind": "cpu"})
    refused = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert refused.status_code == 422 and "cpupower" in refused.text
    body["algorithm"] = "rx/0"
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    assert created.json()["algorithm"] == "randomx"
    body["algorithm"] = "cpupower"
    refused_update = client.put("/api/flight-sheets/" + created.json()["id"], headers=auth(admin), json=body)
    assert refused_update.status_code == 422 and "cpupower" in refused_update.text
    saved = next(x for x in client.get("/api/flight-sheets", headers=auth(admin)).json() if x["id"] == created.json()["id"])
    assert saved["algorithm"] == "randomx"


def test_cpupower_custom_coin_still_supports_integrated_CPU_GPU_dual(setup):
    client, admin, wallet = setup
    body = payload(wallet("CPUCHAIN"), version="3.7.1")
    body.update(name="Custom CPU chain + GPU", coin="CPUCHAIN", algorithm="cpupower", extra_config={"resource_kind": "cpu", "cpu_threads": 4}, secondary={"miner_type": "srbminer", "miner_version": "3.7.1", "coin": "XEL", "wallet_id": wallet("XEL"), "algorithm": "xelishashv3", "pool_url": "stratum+tcp://gpu.test:3333", "device_indices": [0], "extra_config": {"resource_kind": "gpu"}})
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    assert created.json()["algorithm"] == "cpupower"
    assert created.json()["extra_config"]["resource_kind"] == "cpu"
    assert created.json()["secondary"]["extra_config"]["resource_kind"] == "gpu"


def test_periodic_EPIC_CPU_with_actual_XMR_RandomX_coin_is_preserved(setup):
    client, admin, wallet = setup
    body = payload(wallet("EPIC"), version="3.7.1")
    body.update(name="Periodic EPIC + XMR", coin="EPIC", algorithm="randomepic", extra_config={"resource_kind": "cpu"}, secondary={"miner_type": "srbminer", "miner_version": "3.7.1", "coin": "XMR", "wallet_id": wallet("XMR"), "algorithm": "rx", "pool_url": "stratum+tcp://xmr.test:3333", "extra_config": {"resource_kind": "cpu"}})
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    assert created.json()["algorithm"] == "randomepic"
    assert created.json()["secondary"]["algorithm"] == "randomx"



def test_release_372_accepts_K1_extra_args_without_guessing_GPU_identity(setup):
    client, admin, wallet = setup
    body = payload(wallet("PEARL"), version="3.7.2")
    body.update(coin="PEARL", algorithm="pearlhash", extra_config={"extra_args": '--pearl-k1\n--future-kernel-mode "literal value"'})
    created = client.post("/api/flight-sheets", headers=auth(admin), json=body)
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["miner_version"] == "3.7.2"
    assert data["extra_config"]["extra_args"] == body["extra_config"]["extra_args"]
    assert not data["device_indices"]
    catalog = next(a for a in client.get("/api/miner-adapters", headers=auth(admin)).json() if a["miner_type"] == "srbminer")
    assert catalog["catalogue"]["version"] == "3.7.2"
    assert "3.4.6" in catalog["release"]["fallback_versions"]
    assert MINER_ADAPTERS.require("srbminer").release.normalize_version("3.4.6") == "3.4.6"
