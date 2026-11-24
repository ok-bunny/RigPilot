import json
import pytest
from server.schemas import AgentGpuOcApply, AgentGpuOcBatchApply, FlightSheetCreate
from server.main import _render_worker_name
from test_device_gpu_oc import (
    client, login, auth, enroll, gpu_heartbeat, post_result, apply_payload,
    gpu_inventory_heartbeat, gpu_oc_fault_result, desired_oc,
)

FIELDS=['power_limit_w','core_clock_offset_mhz','core_clock_lock_mhz','memory_clock_offset_mhz','memory_clock_lock_mhz','fan_percent']

def seeded(client):
    token=login(client);aid,secret=enroll(client,token);gpu_heartbeat(client,aid,secret)
    endpoint=f'/api/agents/{aid}/gpu-oc/apply';first=client.post(endpoint,headers=auth(token),json=apply_payload());assert first.status_code==201,first.text
    command=gpu_heartbeat(client,aid,secret).json()['commands'][0]
    assert post_result(client,aid,secret,command['id'],gpu_oc_command=command).status_code==200
    return token,aid,secret,endpoint

def desired(client,aid):
    with client.app.state.db.connection() as db:
        return json.loads(db.execute('SELECT desired_gpu_oc_json FROM agents WHERE id=?',(aid,)).fetchone()[0])

@pytest.mark.parametrize('field',FIELDS)
def test_explicit_null_removes_only_requested_field_and_uses_old_replace_wire(client,field):
    token,aid,secret,endpoint=seeded(client);before=desired(client,aid)[0]['setting']
    response=client.post(endpoint,headers=auth(token),json={'vendor':'nvidia','gpu_uuid':'GPU-5080-stable-uuid','setting':{'index':2,field:None}})
    assert response.status_code==201,response.text;assert response.json()['deferred'] is False
    command=gpu_heartbeat(client,aid,secret).json()['commands'][0]
    expected={k:v for k,v in before.items() if k!=field}
    assert command['payload']['mode']=='replace_one';assert command['payload']['settings']==[expected]
    assert 'reset_fields' not in command['payload'];assert all(v is not None for v in expected.values())
    assert desired(client,aid)[0]['setting']==expected
    assert post_result(client,aid,secret,command['id'],gpu_oc_command=command).status_code==200

@pytest.mark.parametrize('success',[True,False])
def test_all_null_uses_old_full_reset_and_deletes_saved_record_only_on_success(client,success):
    token,aid,secret,endpoint=seeded(client);before=desired(client,aid)
    response=client.post(endpoint,headers=auth(token),json={'vendor':'nvidia','gpu_uuid':'GPU-5080-stable-uuid','setting':{'index':2,**{f:None for f in FIELDS}}})
    assert response.status_code==201,response.text;assert desired(client,aid)==before
    command=gpu_heartbeat(client,aid,secret).json()['commands'][0]
    assert command['payload']['mode']=='reset_one';assert command['payload']['settings']==[before[0]['setting']]
    result=post_result(client,aid,secret,command['id'],gpu_oc_command=command,status='succeeded' if success else 'failed')
    assert result.status_code==200,result.text;assert desired(client,aid)==([] if success else before)

def test_clear_when_miner_stopped_is_sent_immediately_and_omitted_and_zero_are_distinct(client):
    token,aid,secret,endpoint=seeded(client);gpu_heartbeat(client,aid,secret,mining_state='stopped')
    before=desired(client,aid)[0]['setting']
    response=client.post(endpoint,headers=auth(token),json={'vendor':'nvidia','gpu_uuid':'GPU-5080-stable-uuid','setting':{'index':2,'fan_percent':None,'core_clock_offset_mhz':0}})
    assert response.status_code==201,response.text;assert response.json()['deferred'] is False
    target=desired(client,aid)[0]['setting'];assert 'fan_percent' not in target;assert target['core_clock_offset_mhz']==0
    assert target['memory_clock_offset_mhz']==before['memory_clock_offset_mhz']
    command=gpu_heartbeat(client,aid,secret,mining_state='stopped').json()['commands'][0]
    assert command['payload']['mode']=='replace_one';assert command['payload']['settings']==[target]

def test_clear_schema_keeps_unset_null_and_numeric_zero_separate():
    request=AgentGpuOcApply.model_validate({'vendor':'nvidia','gpu_uuid':'GPU-test','setting':{'index':0,'core_clock_offset_mhz':0,'fan_percent':None}})
    assert request.setting.model_dump(exclude_unset=True)=={'index':0,'core_clock_offset_mhz':0,'fan_percent':None}
    with pytest.raises(ValueError):AgentGpuOcApply.model_validate({'vendor':'nvidia','gpu_uuid':'GPU-test','setting':{'index':0}})
    batch=AgentGpuOcBatchApply.model_validate({'vendor':'nvidia','gpu_uuids':['GPU-test'],'setting':{'fan_percent':None}})
    assert batch.setting.model_dump(exclude_unset=True)=={'fan_percent':None}
    with pytest.raises(ValueError):AgentGpuOcBatchApply.model_validate({'vendor':'nvidia','gpu_uuids':['GPU-test'],'setting':{}})

@pytest.mark.parametrize('batch', [False, True], ids=['single', 'batch'])
def test_core_lock_schema_preserves_shared_integer_input_bounds(batch):
    model = AgentGpuOcBatchApply if batch else AgentGpuOcApply
    target = {'gpu_uuids': ['GPU-test']} if batch else {'gpu_uuid': 'GPU-test'}
    index = {} if batch else {'index': 0}
    for mhz in [100, 5000]:
        request = model.model_validate({'vendor': 'nvidia', **target,
            'setting': {**index, 'core_clock_lock_mhz': mhz}})
        assert request.setting.model_dump(exclude_unset=True) == {**index, 'core_clock_lock_mhz': mhz}
    for invalid in [99, 5001, 2490.5]:
        with pytest.raises(ValueError):
            model.model_validate({'vendor': 'nvidia', **target,
                'setting': {**index, 'core_clock_lock_mhz': invalid}})

def test_controller_worker_stage_is_canonical_and_does_not_reparse_inserted_names():
    template='{hostname}'*12
    assert len(template)==120
    normalized=FlightSheetCreate.validate_worker_template(template)
    assert normalized=='%WORKER_NAME%'*12 and len(normalized)==156
    assert _render_worker_name(normalized,{'name':'x','hostname':'os','id':'id'})=='x'*12
    assert _render_worker_name('%work_name%/{system_hostname}/{agent_id}',{'name':'%WAL%{hostname}','hostname':'os','id':'id'})=='%WAL%{hostname}/os/id'


def test_batch_clear_rolls_back_first_card_if_later_card_has_no_saved_setting(client, monkeypatch):
    from server.database import Database
    from test_device_gpu_oc import gpu_inventory_heartbeat
    token, aid, secret, _endpoint = seeded(client)
    first_uuid = 'GPU-5080-stable-uuid'
    second_uuid = 'GPU-second-without-saved-oc'
    reported = gpu_inventory_heartbeat(client, aid, secret, [
        {'index': 2, 'name': 'NVIDIA first', 'vendor': 'nvidia', 'uuid': first_uuid,
         'oc_capabilities': FIELDS},
        {'index': 3, 'name': 'NVIDIA second', 'vendor': 'nvidia', 'uuid': second_uuid,
         'oc_capabilities': FIELDS},
    ])
    assert reported.status_code == 200, reported.text

    def snapshot():
        with client.app.state.db.connection() as db:
            return {
                'commands': [dict(row) for row in db.execute('SELECT * FROM commands ORDER BY id')],
                'audit': [dict(row) for row in db.execute('SELECT * FROM audit_logs ORDER BY id')],
                'desired': dict(db.execute('SELECT desired_gpu_oc_json,desired_gpu_oc_pending_json FROM agents WHERE id=?', (aid,)).fetchone()),
            }

    before = snapshot()
    assert [entry['gpu_uuid'] for entry in json.loads(before['desired']['desired_gpu_oc_json'])] == [first_uuid]
    original_audit = Database.audit
    queued_before_error = []

    def observe_audit(db, **kwargs):
        result = original_audit(db, **kwargs)
        details = kwargs.get('details') or {}
        if kwargs.get('action') == 'desired_gpu_oc_saved' and details.get('cleared_fields') == ['power_limit_w']:
            assert details['gpu_uuid'] == first_uuid
            queued = db.execute("SELECT id,payload_json FROM commands WHERE agent_id=? AND action='apply_gpu_profile' AND status='queued'", (aid,)).fetchall()
            assert len(queued) == 1
            payload = json.loads(queued[0]['payload_json'])
            assert payload['expected_gpu_uuid'] == first_uuid and payload['mode'] == 'replace_one'
            assert 'power_limit_w' not in payload['settings'][0]
            assert db.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0] > len(before['audit'])
            queued_before_error.append(queued[0]['id'])
        return result

    monkeypatch.setattr(Database, 'audit', staticmethod(observe_audit))
    response = client.post(f'/api/agents/{aid}/gpu-oc/batch-apply', headers=auth(token), json={
        'vendor': 'nvidia', 'gpu_uuids': [first_uuid, second_uuid],
        'setting': {'power_limit_w': None},
    })
    assert response.status_code == 409, response.text
    assert '没有控制端已保存' in response.json()['detail']
    assert len(queued_before_error) == 1, 'the first card must actually queue before the second fails'
    assert snapshot() == before, 'all queued commands, audits and desired/pending state must roll back'


def two_card_fault(client):
    token = login(client)
    aid, secret = enroll(client, token, version='0.8.47')
    gpus = [{'index': index, 'uuid': f'GPU-hold-{index}', 'vendor': 'nvidia',
             'name': f'NVIDIA synthetic {index}', 'oc_capabilities': ['power_limit_w']}
            for index in range(2)]

    def heartbeat(reported=None):
        response = gpu_inventory_heartbeat(client, aid, secret,
            gpus if reported is None else reported, version='0.8.47')
        assert response.status_code == 200, response.text
        return response.json()['commands']

    assert heartbeat() == []
    for gpu in gpus:
        saved = client.post(f'/api/agents/{aid}/gpu-oc/apply', headers=auth(token), json={
            'vendor': 'nvidia', 'gpu_uuid': gpu['uuid'],
            'setting': {'index': gpu['index'], 'power_limit_w': 150 + gpu['index']},
        })
        assert saved.status_code == 201, saved.text
        command = heartbeat()[0]
        assert post_result(client, aid, secret, command['id'], gpu_oc_command=command).status_code == 200
    restart = client.post(f'/api/agents/{aid}/commands', headers=auth(token), json={'action': 'restart_mining'})
    assert restart.status_code == 201, restart.text
    assert heartbeat()[0]['id'] == restart.json()['id']
    assert post_result(client, aid, secret, restart.json()['id'], result={'restarted': True}).status_code == 200
    commands = heartbeat()
    by_uuid = {item['payload']['expected_gpu_uuid']: item for item in commands}
    failed = by_uuid[gpus[0]['uuid']]
    assert post_result(client, aid, secret, failed['id'], status='failed',
        result=gpu_oc_fault_result(failed)).status_code == 200
    other = by_uuid[gpus[1]['uuid']]
    assert post_result(client, aid, secret, other['id'], gpu_oc_command=other).status_code == 200
    assert heartbeat() == []
    entries = {entry['gpu_uuid']: entry for entry in desired_oc(client, token, aid)}
    assert entries[gpus[0]['uuid']]['replay_block']['reason'] == 'hardware_reset_required'
    assert 'replay_block' not in entries[gpus[1]['uuid']]
    return token, aid, secret, gpus, heartbeat, entries


@pytest.mark.parametrize('operation', ['reset', 'forget'])
def test_gpu_oc_block_cleanup_is_uuid_scoped_and_failed_reset_preserves_it(client, operation):
    token, aid, secret, gpus, heartbeat, before = two_card_fault(client)
    endpoint = f'/api/agents/{aid}/gpu-oc/0'
    params = {'gpu_uuid': gpus[0]['uuid']}
    if operation == 'forget':
        assert heartbeat([gpus[1]]) == []
        params['forget_stale'] = True
    deleted = client.delete(endpoint, headers=auth(token), params=params)
    assert deleted.status_code == 200, deleted.text
    if operation == 'reset':
        reset = heartbeat()[0]
        assert reset['payload']['mode'] == 'reset_one'
        assert post_result(client, aid, secret, reset['id'], status='failed').status_code == 200
        assert {entry['gpu_uuid']: entry for entry in desired_oc(client, token, aid)} == before
        retry = client.delete(endpoint, headers=auth(token), params=params)
        assert retry.status_code == 200, retry.text
        reset = heartbeat()[0]
        assert reset['id'] != deleted.json()['reset_command']['id']
        assert post_result(client, aid, secret, reset['id'], gpu_oc_command=reset).status_code == 200
    else:
        assert deleted.json()['reset_command'] is None
        assert deleted.json()['status'] == 'deleted'
    assert desired_oc(client, token, aid) == [before[gpus[1]['uuid']]]
    assert heartbeat(gpus if operation == 'reset' else [gpus[1]]) == []


def test_gpu_oc_unsafe_failed_reset_holds_saved_intent_until_explicit_reset_retry_succeeds(client):
    token, aid, secret, _endpoint = seeded(client)
    assert gpu_heartbeat(client, aid, secret, version='0.8.47').status_code == 200
    before = desired_oc(client, token, aid)[0]
    endpoint = f'/api/agents/{aid}/gpu-oc/2'
    params = {'gpu_uuid': 'GPU-5080-stable-uuid'}
    deleted = client.delete(endpoint, headers=auth(token), params=params)
    assert deleted.status_code == 200, deleted.text
    command = gpu_heartbeat(client, aid, secret, version='0.8.47').json()['commands'][0]
    assert command['payload']['mode'] == 'reset_one'
    failed = post_result(client, aid, secret, command['id'], status='failed',
        result=gpu_oc_fault_result(command))
    assert failed.status_code == 200, failed.text
    entry = desired_oc(client, token, aid)[0]
    assert entry['setting'] == before['setting'] and entry['intent_id'] == before['intent_id']
    assert entry['replay_block']['reason'] == 'hardware_reset_required'
    assert gpu_heartbeat(client, aid, secret, version='0.8.47').json()['commands'] == []
    retried = client.delete(endpoint, headers=auth(token), params=params)
    assert retried.status_code == 200, retried.text
    command = gpu_heartbeat(client, aid, secret, version='0.8.47').json()['commands'][0]
    assert post_result(client, aid, secret, command['id'], gpu_oc_command=command).status_code == 200
    assert desired_oc(client, token, aid) == []


def test_gpu_oc_new_save_for_another_card_preserves_the_first_card_hold(client):
    token, aid, secret, gpus, heartbeat, before = two_card_fault(client)
    saved = client.post(f'/api/agents/{aid}/gpu-oc/apply', headers=auth(token), json={
        'vendor': 'nvidia', 'gpu_uuid': gpus[1]['uuid'], 'setting': before[gpus[1]['uuid']]['setting'],
    })
    assert saved.status_code == 201, saved.text
    after = {entry['gpu_uuid']: entry for entry in desired_oc(client, token, aid)}
    assert after[gpus[0]['uuid']] == before[gpus[0]['uuid']]
    assert after[gpus[1]['uuid']]['intent_id'] != before[gpus[1]['uuid']]['intent_id']
    commands = heartbeat()
    assert len(commands) == 1 and commands[0]['payload']['expected_gpu_uuid'] == gpus[1]['uuid']
    assert post_result(client, aid, secret, commands[0]['id'], gpu_oc_command=commands[0]).status_code == 200
    assert heartbeat() == []
