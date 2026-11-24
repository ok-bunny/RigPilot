"""Controller update completion must preserve desired per-card OC."""
import json
from datetime import datetime, timedelta, timezone
import pytest
from test_agent_releases import (
    ADMIN_PASSWORD, auth, enroll_update_agent, linux_update_tar,
    login, release_app, upload,
)


@pytest.fixture()
def update_oc(release_app, request):
    client, settings = release_app
    token = login(client, 'admin', ADMIN_PASSWORD)
    uploaded = upload(client, token, linux_update_tar(version='0.8.19'),
        version='0.8.19', platform='linux',
        file_name='RigPilotAgent-0.8.19-Ubuntu-x86_64-OnlineUpdate.tar.gz')
    assert uploaded.status_code == 201, uploaded.text
    release = uploaded.json()
    agent_id, secret = enroll_update_agent(client, token, os_family='linux',
        version='0.8.17', capabilities=['agent_update.v1'])
    headers = {'X-Agent-ID': agent_id, **auth(secret)}
    desired = [{'gpu_uuid': f'GPU-update-{i}', 'name': f'card-{i}',
        'setting': {'index': i, 'power_limit_w': 250, 'core_clock_offset_mhz': 300}}
        for i in range(4)]
    sample = [datetime.now(timezone.utc) - timedelta(seconds=30)]
    def heartbeat(version='0.8.19', state='running', sheet='original-flight', gpus=None, fresh=True, checked_at='auto', verified=None):
        if fresh:
            sample[0] += timedelta(seconds=1)
        observed = sample[0].isoformat() if checked_at == 'auto' else checked_at
        response = client.post('/api/agent/heartbeat', headers=headers, json={
            'hostname': 'update-canary.local', 'os_family': 'linux',
            'agent_version': version, 'capabilities': ['agent_update.v1'],
            'inventory': {'architecture': 'AMD64'},
            'metrics': {'mining_status': {'configured': True, 'state': state,
                'process_verified': verified if verified is not None else state in {'running', 'active'}, 'flight_sheet_id': sheet,
                'miner_type': 'srbminer-multi', 'checked_at': observed},
                'gpus': gpus if gpus is not None else [
                    {'uuid': entry['gpu_uuid'], 'index': i, 'vendor': 'nvidia',
                     'name': entry['name'], 'oc_capabilities': ['power_limit_w', 'core_clock_offset_mhz']}
                    for i, entry in enumerate(desired)]}})
        assert response.status_code == 200, response.text
        return response.json()['commands']
    with client.app.state.db.connection() as db:
        creator = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        db.execute("INSERT INTO wallets(id,name,coin,address,created_by,created_at,updated_at) VALUES('oc-wallet','wallet','PEARL','synthetic-wallet',?,datetime('now'),datetime('now'))", (creator,))
        db.execute("INSERT INTO flight_sheets(id,name,miner_type,coin,wallet_id,pool_url,miner_profile,worker_name_template,created_by,created_at,updated_at) VALUES('original-flight','flight','srbminer-multi','PEARL','oc-wallet','stratum+tcp://localhost:3333','default','%WORKER_NAME%',?,datetime('now'),datetime('now'))", (creator,))
    assert heartbeat(version='0.8.17') == []
    with client.app.state.db.connection() as db:
        db.execute('UPDATE agents SET desired_gpu_oc_json=?, desired_gpu_oc_pending_json=? WHERE id=?',
                   (json.dumps(desired), '[]', agent_id))
    options = getattr(request, 'param', None)
    if isinstance(options, str):
        assert heartbeat(version='0.8.17', state=options, verified=True) == []
    pre_update_fault = None
    if isinstance(options, dict) and options.get('pre_update_fault'):
        from test_device_gpu_oc import gpu_oc_fault_result
        saved = client.post(f'/api/agents/{agent_id}/gpu-oc/apply', headers=auth(token), json={
            'vendor': 'nvidia', 'gpu_uuid': desired[0]['gpu_uuid'], 'setting': desired[0]['setting'],
        })
        assert saved.status_code == 201, saved.text
        prior_commands = heartbeat(version='0.8.17')
        assert len(prior_commands) == 1
        pre_update_fault = prior_commands[0]
        receipt = client.post(f"/api/agent/commands/{pre_update_fault['id']}/result", headers=headers, json={
            'status': 'failed', 'result': gpu_oc_fault_result(pre_update_fault),
            'error': 'synthetic NVML reset required',
        })
        assert receipt.status_code == 200, receipt.text
        assert heartbeat(version='0.8.17') == []
    deployed = client.post(f"/api/agent-releases/{release['id']}/deploy",
        headers=auth(token), json={'agent_id': agent_id})
    assert deployed.status_code == 201, deployed.text
    command_id = deployed.json()['command_id']
    def result(body):
        return client.post(f'/api/agent/commands/{command_id}/result', headers=headers, json=body)
    def profiles():
        with client.app.state.db.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM commands WHERE agent_id=? AND action='apply_gpu_profile' ORDER BY rowid", (agent_id,))]
    def pending():
        with client.app.state.db.connection() as db:
            return json.loads(db.execute('SELECT desired_gpu_oc_pending_json FROM agents WHERE id=?', (agent_id,)).fetchone()[0])
    assert [item['id'] for item in heartbeat(version='0.8.17')] == [command_id]
    staged = {'status': 'succeeded', 'result': {'state': 'staged',
        'release_id': release['id'], 'version': '0.8.19', 'sha256': release['sha256'],
        'activation_id': 'a'*32, 'activation_scheduled': True,
        'restart_required': True, 'miner_continuity_checked': True}}
    final = {'status': 'succeeded', 'result': {'activation_confirmed': True,
        'activation_id': 'a'*32, 'version': '0.8.19', 'health_heartbeats': 2}}
    return locals()


def test_update_replays_four_desired_cards_only_after_final_activation(update_oc):
    case = update_oc
    assert case['profiles']() == [] and case['pending']() == []
    assert case['result'](case['staged']).json() == {'status': 'running'}
    assert case['profiles']() == [] and case['pending']() == []
    assert case['heartbeat']() == []
    assert case['result'](case['final']).status_code == 409
    assert case['profiles']() == [] and case['pending']() == []
    assert case['heartbeat']() == []
    confirmed = case['result'](case['final'])
    assert confirmed.status_code == 200, confirmed.text
    assert case['profiles']() == [] and case['pending']() == []
    assert case['heartbeat'](fresh=False) == []
    assert len(case['heartbeat']()) == 4
    assert len(case['profiles']()) == 4
    assert set(case['pending']()) == {entry['gpu_uuid'] for entry in case['desired']}
    assert case['result'](case['final']).json() == {'status': 'succeeded'}
    assert len(case['profiles']()) == 4


@pytest.mark.parametrize('signal', ['wrong_activation', 'wrong_version', 'boolean_count',
    'stopped', 'unverified', 'different_flight', 'rolled_back'])
def test_update_does_not_replay_without_valid_completion_and_same_live_miner(update_oc, signal):
    case = update_oc
    assert case['result'](case['staged']).json() == {'status': 'running'}
    for _ in range(2):
        case['heartbeat'](state='stopped' if signal == 'stopped' else 'running',
            sheet='different-flight' if signal == 'different_flight' else 'original-flight')
    if signal == 'unverified':
        with case['client'].app.state.db.connection() as db:
            row = db.execute('SELECT metrics_json FROM agents WHERE id=?', (case['agent_id'],)).fetchone()
            metrics = json.loads(row[0]); metrics['mining_status']['process_verified'] = False
            db.execute('UPDATE agents SET metrics_json=? WHERE id=?', (json.dumps(metrics), case['agent_id']))
    final = json.loads(json.dumps(case['final']))
    if signal == 'wrong_activation': final['result']['activation_id'] = 'b'*32
    if signal == 'wrong_version': final['result']['version'] = '0.8.18'
    if signal == 'boolean_count': final['result']['health_heartbeats'] = True
    if signal == 'rolled_back':
        final = {'status': 'failed', 'result': {'activation_id': 'a'*32,
            'version': '0.8.19', 'rolled_back': True}, 'error': 'synthetic rollback'}
    response = case['result'](final)
    assert response.status_code == (409 if signal in {'wrong_activation','wrong_version','boolean_count'} else 200), response.text
    assert case['profiles']() == []
    assert case['pending']() == []


def confirm_update(case):
    assert case['result'](case['staged']).json() == {'status': 'running'}
    assert case['heartbeat']() == []
    assert case['heartbeat']() == []
    assert case['result'](case['final']).json() == {'status': 'succeeded'}


def recovery(case):
    with case['client'].app.state.db.connection() as db:
        return json.loads(db.execute('SELECT oc_recovery_json FROM agent_update_jobs WHERE command_id=?',
            (case['command_id'],)).fetchone()[0])


@pytest.mark.parametrize('action', ['stop_mining','remove_flight_sheet','restart_mining','reboot','shutdown','apply_flight_sheet'])
@pytest.mark.parametrize('terminal', [False, True])
def test_late_intent_permanently_cancels_update_oc(update_oc, action, terminal):
    case = update_oc
    confirm_update(case)
    # These later intents may already be terminal when the next sample arrives.
    # Use the actual stop endpoint; other actions are durable command fixtures
    # so unrelated miner-capability gates do not obscure ordering behavior.
    if action == 'stop_mining':
        queued = case['client'].post(f"/api/agents/{case['agent_id']}/commands",
            headers=auth(case['token']), json={'action': action})
        assert queued.status_code == 201, queued.text
        later_id = queued.json()['id']
    else:
        later_id = 'later-intent'
        with case['client'].app.state.db.connection() as db:
            db.execute("INSERT INTO commands(id,agent_id,action,status,created_by,created_at,expires_at) VALUES(?,?,?,'queued',?,datetime('now'),?)",
                (later_id, case['agent_id'], action, case['creator'], (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()))
    if terminal:
        with case['client'].app.state.db.connection() as db:
            db.execute("UPDATE commands SET status='succeeded', finished_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), later_id))
    case['heartbeat']()
    assert case['profiles']() == [] and case['pending']() == []
    assert recovery(case)['state'] == 'cancelled'
    with case['client'].app.state.db.connection() as db:
        db.execute('DELETE FROM commands WHERE id=?', (later_id,))
    case['result'](case['final'])
    case['heartbeat']()
    assert case['profiles']() == [] and recovery(case)['state'] == 'cancelled'


@pytest.mark.parametrize('change', ['same_flight_config','wallet','oc_intent','edited_then_reverted','cleared_stop_history'])
def test_changed_intent_cannot_be_revived_by_later_matching_telemetry(update_oc, change):
    from server.database import Database
    case = update_oc
    confirm_update(case)
    with case['client'].app.state.db.connection() as db:
        if change == 'same_flight_config':
            db.execute("UPDATE flight_sheets SET pool_url='stratum+tcp://localhost:4444' WHERE id='original-flight'")
        elif change == 'wallet':
            db.execute("UPDATE wallets SET address='synthetic-new-wallet' WHERE id='oc-wallet'")
        elif change == 'oc_intent':
            modified = json.loads(json.dumps(case['desired'])); modified[0]['setting']['core_clock_offset_mhz'] = 200
            db.execute('UPDATE agents SET desired_gpu_oc_json=? WHERE id=?', (json.dumps(modified), case['agent_id']))
        elif change == 'edited_then_reverted':
            Database.audit(db, actor='admin', action='flight_sheet_updated', target_type='flight_sheet', target_id='original-flight', details={})
        else:
            Database.audit(db, actor='admin', action='command_queued', target_type='agent', target_id=case['agent_id'], details={'command_id':'cleared-history-stop','action':'stop_mining'})
    assert case['heartbeat']() == []
    assert recovery(case)['state'] == 'cancelled'
    assert case['pending']() == []


def test_missing_inventory_retries_by_uuid_and_acknowledgements_consume_event(update_oc):
    from test_device_gpu_oc import gpu_oc_success_result
    case = update_oc
    confirm_update(case)
    assert case['heartbeat'](gpus=[]) == []
    assert len(case['pending']()) == 4 and recovery(case)['state'] == 'pending'
    swapped = [{'uuid': entry['gpu_uuid'], 'index': 3-i, 'vendor': 'nvidia'} for i, entry in enumerate(case['desired'])]
    commands = case['heartbeat'](gpus=swapped)
    assert len(commands) == 4
    for command in commands:
        payload = command['payload']; original = int(payload['expected_gpu_uuid'].rsplit('-',1)[1])
        assert payload['settings'][0]['index'] == 3-original
        assert payload['settings'][0]['core_clock_offset_mhz'] == 300
    # In-flight commands must keep the pending UUIDs and not create duplicates.
    case['heartbeat'](gpus=swapped)
    assert len(case['profiles']()) == 4 and len(case['pending']()) == 4
    for command in commands:
        response = case['client'].post(f"/api/agent/commands/{command['id']}/result", headers=case['headers'],
            json={'status':'succeeded', 'result':gpu_oc_success_result(command)})
        assert response.status_code == 200, response.text
    assert case['pending']() == []
    case['heartbeat'](gpus=swapped)
    assert recovery(case)['state'] == 'complete'
    case['result'](case['final']); case['heartbeat'](gpus=swapped)
    assert len(case['profiles']()) == 4


def test_controller_restart_keeps_recovery_and_cached_sample_cannot_unlock_it(update_oc):
    from fastapi.testclient import TestClient
    from server.main import create_app
    case = update_oc
    confirm_update(case)
    # Open a fresh app over the same durable DB, with no in-memory event state.
    client = TestClient(create_app(case['settings']))
    with case['client'].app.state.db.connection() as db:
        row = dict(db.execute('SELECT * FROM agents WHERE id=?', (case['agent_id'],)).fetchone())
    body = {'hostname':row['hostname'], 'os_family':'linux', 'agent_version':'0.8.19',
        'capabilities':['agent_update.v1'], 'inventory':json.loads(row['inventory_json']), 'metrics':json.loads(row['metrics_json'])}
    assert client.post('/api/agent/heartbeat', headers=case['headers'], json=body).json()['commands'] == []
    body['metrics']['mining_status']['checked_at'] = (case['sample'][0]+timedelta(seconds=1)).isoformat()
    response = client.post('/api/agent/heartbeat', headers=case['headers'], json=body)
    assert response.status_code == 200, response.text
    assert len(response.json()['commands']) == 4
    client.close()


@pytest.mark.parametrize('signal', ['missing_checked_at','malformed_checked_at','regressed_checked_at','stale_checked_at','different_miner','stopped','different_flight'])
def test_only_fresh_matching_process_observation_unlocks_recovery(update_oc, signal):
    case = update_oc
    confirm_update(case)
    checked = {'missing_checked_at':None,'malformed_checked_at':42,
        'regressed_checked_at':(case['sample'][0]-timedelta(seconds=1)).isoformat(),
        'stale_checked_at':(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()}.get(signal,'auto')
    if signal == 'different_miner':
        with case['client'].app.state.db.connection() as db:
            row = db.execute('SELECT metrics_json FROM agents WHERE id=?',(case['agent_id'],)).fetchone()
            metrics=json.loads(row[0]);metrics['mining_status']['miner_type']='other-miner'
            metrics['mining_status']['checked_at']=(case['sample'][0]+timedelta(seconds=1)).isoformat()
        response=case['client'].post('/api/agent/heartbeat',headers=case['headers'],json={
            'hostname':'update-canary.local','os_family':'linux','agent_version':'0.8.19',
            'capabilities':['agent_update.v1'],'metrics':metrics})
        assert response.status_code==200,response.text
        assert response.json()['commands']==[]
    else:
        assert case['heartbeat'](checked_at=checked,
            state='stopped' if signal=='stopped' else 'running',
            sheet='new-flight' if signal=='different_flight' else 'original-flight') == []
    assert case['profiles']() == [] and case['pending']() == []


def test_recovery_expires_without_a_late_fleet_replay(update_oc):
    case=update_oc;confirm_update(case)
    with case['client'].app.state.db.connection() as db:
        context=recovery(case);context['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        db.execute('UPDATE agent_update_jobs SET oc_recovery_json=? WHERE command_id=?',(json.dumps(context),case['command_id']))
    assert case['heartbeat']()==[]
    assert recovery(case)['state']=='cancelled'
    case['heartbeat']();assert case['profiles']()==[]


def test_historical_job_without_bound_context_is_never_backfilled(update_oc):
    case=update_oc
    with case['client'].app.state.db.connection() as db:
        db.execute('UPDATE agent_update_jobs SET oc_recovery_json=NULL WHERE command_id=?',(case['command_id'],))
    confirm_update(case)
    assert case['heartbeat']()==[] and case['pending']()==[]


def test_late_stop_after_missing_inventory_does_not_revive_old_oc_success(update_oc):
    case=update_oc
    # Model the successful OC command preceding the update in the actual incident.
    with case['client'].app.state.db.connection() as db:
        db.execute("INSERT INTO commands(rowid,id,agent_id,action,status,created_by,created_at,expires_at) VALUES(-1,'prior-oc',?,'apply_gpu_profile','succeeded',?,datetime('now'),datetime('now'))",(case['agent_id'],case['creator']))
    confirm_update(case)
    case['heartbeat'](gpus=[]);assert len(case['pending']())==4
    stop=case['client'].post(f"/api/agents/{case['agent_id']}/commands",headers=auth(case['token']),json={'action':'stop_mining'})
    assert stop.status_code==201,stop.text
    commands=case['heartbeat']();assert [c['action'] for c in commands]==['stop_mining']
    stopped=case['client'].post(f"/api/agent/commands/{stop.json()['id']}/result",headers=case['headers'],json={'status':'succeeded','result':{}})
    assert stopped.status_code==200,stopped.text
    assert len(case['pending']())==4  # retained for an explicit future start
    assert case['heartbeat']()==[]   # cached running must not undo the later stop
    assert len(case['profiles']())==1


def test_migration_adds_nullable_context_without_backfilling_existing_job(update_oc):
    from server.database import Database
    case=update_oc
    with case['client'].app.state.db.connection() as db:
        db.execute('ALTER TABLE agent_update_jobs DROP COLUMN oc_recovery_json')
        before=[tuple(row) for row in db.execute('SELECT * FROM agent_update_jobs')]
        Database._migrate_agent_update_jobs_table(db)
        after=[tuple(row) for row in db.execute('SELECT * FROM agent_update_jobs')]
        assert [row[:-1] for row in after]==before and all(row[-1] is None for row in after)
        Database._migrate_agent_update_jobs_table(db)
        assert [tuple(row) for row in db.execute('SELECT * FROM agent_update_jobs')]==after


@pytest.mark.parametrize('unsafe', [False, True], ids=['late-transient', 'late-unsafe'])
def test_expired_event_does_not_redeliver_or_rearm_from_late_failed_receipt(update_oc, unsafe):
    from test_device_gpu_oc import desired_oc, gpu_oc_fault_result
    case=update_oc;confirm_update(case)
    commands=case['heartbeat']();assert len(commands)==4
    with case['client'].app.state.db.connection() as db:
        context=recovery(case);context['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        db.execute('UPDATE agent_update_jobs SET oc_recovery_json=? WHERE command_id=?',(json.dumps(context),case['command_id']))
    assert case['heartbeat']()==[] and case['pending']()==[]
    failed=case['client'].post(f"/api/agent/commands/{commands[0]['id']}/result",headers=case['headers'],json={
        'status':'failed', 'result': gpu_oc_fault_result(commands[0]) if unsafe else {},
        'error':'synthetic late failure'})
    assert failed.status_code==200,failed.text
    assert case['pending']()==[] and case['heartbeat']()==[]
    assert len(case['profiles']())==4
    assert recovery(case)['state'] == 'cancelled'
    entries = desired_oc(case['client'], case['token'], case['agent_id'])
    blocked = [entry for entry in entries if 'replay_block' in entry]
    assert [entry['gpu_uuid'] for entry in blocked] == ([commands[0]['payload']['expected_gpu_uuid']] if unsafe else [])


def test_newer_saved_oc_pending_survives_cancellation_of_old_update_event(update_oc):
    from server.database import Database
    case=update_oc;confirm_update(case)
    case['heartbeat'](gpus=[]);assert len(case['pending']())==4
    modified=json.loads(json.dumps(case['desired']));modified[0]['setting']['core_clock_offset_mhz']=200
    with case['client'].app.state.db.connection() as db:
        db.execute('UPDATE agents SET desired_gpu_oc_json=?,desired_gpu_oc_pending_json=? WHERE id=?',
            (json.dumps(modified),json.dumps(['GPU-update-0']),case['agent_id']))
        Database.audit(db,actor='admin',action='desired_gpu_oc_saved',target_type='agent',target_id=case['agent_id'],details={})
    assert case['heartbeat'](state='stopped')==[]
    assert recovery(case)['state']=='cancelled'
    assert case['pending']()==['GPU-update-0']


def test_existing_owned_queue_retains_pending_until_recoverable_failure_finishes(update_oc):
    from test_device_gpu_oc import gpu_oc_success_result
    case=update_oc;confirm_update(case)
    commands=case['heartbeat']();assert len(commands)==4
    for command in commands[:-1]:
        result=case['client'].post(f"/api/agent/commands/{command['id']}/result",headers=case['headers'],json={'status':'succeeded','result':gpu_oc_success_result(command)})
        assert result.status_code==200,result.text
    assert len(case['pending']())==1
    case['heartbeat']();assert len(case['profiles']())==4 and len(case['pending']())==1
    result=case['client'].post(f"/api/agent/commands/{commands[-1]['id']}/result",headers=case['headers'],json={'status':'failed','result':{},'error':'synthetic transient failure'})
    assert result.status_code==200,result.text
    retried=case['heartbeat']()
    assert len(retried)==1 and retried[0]['id']!=commands[-1]['id']
    assert retried[0]['payload']['expected_gpu_uuid']==commands[-1]['payload']['expected_gpu_uuid']
    assert len(case['profiles']())==5 and len(case['pending']())==1


def test_history_clear_preserves_bound_recovery_until_child_receipts_finish(update_oc):
    from test_device_gpu_oc import gpu_oc_success_result
    case=update_oc;confirm_update(case)
    cleared=case['client'].delete('/api/agent-updates/history',headers=auth(case['token']))
    assert cleared.status_code==200 and cleared.json()['update_jobs_deleted']==0
    commands=case['heartbeat']();assert len(commands)==4
    cleared=case['client'].delete('/api/agent-updates/history',headers=auth(case['token']))
    assert cleared.json()['update_jobs_deleted']==0
    for command in commands:
        response=case['client'].post(f"/api/agent/commands/{command['id']}/result",headers=case['headers'],json={'status':'succeeded','result':gpu_oc_success_result(command)})
        assert response.status_code==200,response.text
    case['heartbeat']();assert recovery(case)['state']=='complete'
    cleared=case['client'].delete('/api/agent-updates/history',headers=auth(case['token']))
    assert cleared.json()['update_jobs_deleted']==1


@pytest.mark.parametrize('update_oc', ['stopped', 'failed', 'starting'], indirect=True)
def test_verified_non_running_report_cannot_capture_update_oc(update_oc):
    case = update_oc
    with case['client'].app.state.db.connection() as db:
        assert db.execute('SELECT oc_recovery_json FROM agent_update_jobs WHERE command_id=?',
            (case['command_id'],)).fetchone()[0] is None
    confirm_update(case)
    assert case['heartbeat']() == []
    assert case['pending']() == [] and case['profiles']() == []


@pytest.mark.parametrize('state', ['stopped', 'failed', 'starting'])
def test_verified_non_running_report_cannot_unlock_post_final_oc(update_oc, state):
    case = update_oc
    confirm_update(case)
    assert case['heartbeat'](state=state, verified=True) == []
    assert recovery(case)['state'] == 'waiting'
    assert case['pending']() == [] and case['profiles']() == []


def test_verified_active_report_can_unlock_post_final_oc(update_oc):
    case = update_oc
    confirm_update(case)
    assert len(case['heartbeat'](state='active', verified=True)) == 4


@pytest.mark.parametrize('update_oc', [{'pre_update_fault': True}], indirect=True)
def test_update_recovery_cannot_revive_a_previously_fault_blocked_gpu_intent(update_oc):
    from test_device_gpu_oc import desired_oc, gpu_oc_success_result
    case = update_oc
    blocked_uuid = case['pre_update_fault']['payload']['expected_gpu_uuid']
    before = {entry['gpu_uuid']: entry for entry in desired_oc(case['client'], case['token'], case['agent_id'])}
    assert before[blocked_uuid]['replay_block']['reason'] == 'hardware_reset_required'
    prior_count = len(case['profiles']())
    confirm_update(case)
    commands = case['heartbeat']()
    assert len(commands) == 3
    assert {item['payload']['expected_gpu_uuid'] for item in commands} == set(before) - {blocked_uuid}
    for command in commands:
        assert '_controller_gpu_oc_intent' not in command['payload']
        receipt = case['client'].post(f"/api/agent/commands/{command['id']}/result", headers=case['headers'],
            json={'status': 'succeeded', 'result': gpu_oc_success_result(command)})
        assert receipt.status_code == 200, receipt.text
    assert case['heartbeat']() == []
    assert len(case['profiles']()) == prior_count + 3
    after = {entry['gpu_uuid']: entry for entry in desired_oc(case['client'], case['token'], case['agent_id'])}
    assert after[blocked_uuid] == before[blocked_uuid]
    assert recovery(case)['state'] == 'blocked'
    assert recovery(case)['blocked_gpu_uuids'] == [blocked_uuid]


def test_update_owned_gpu_fault_is_not_retried_or_misreported_as_complete(update_oc):
    from test_device_gpu_oc import desired_oc, gpu_oc_fault_result, gpu_oc_success_result
    case = update_oc
    confirm_update(case)
    commands = case['heartbeat']()
    assert len(commands) == 4
    failed_command = commands[0]
    failed_uuid = failed_command['payload']['expected_gpu_uuid']
    for command in commands:
        failed = command['id'] == failed_command['id']
        receipt = case['client'].post(f"/api/agent/commands/{command['id']}/result", headers=case['headers'], json={
            'status': 'failed' if failed else 'succeeded',
            'result': gpu_oc_fault_result(command) if failed else gpu_oc_success_result(command),
            'error': 'synthetic hardware fault' if failed else None,
        })
        assert receipt.status_code == 200, receipt.text
    for _ in range(2):
        assert case['heartbeat']() == []
    assert len(case['profiles']()) == 4
    entries = {entry['gpu_uuid']: entry for entry in desired_oc(case['client'], case['token'], case['agent_id'])}
    assert entries[failed_uuid]['replay_block']['reason'] == 'hardware_reset_required'
    assert all('replay_block' not in entry for uuid, entry in entries.items() if uuid != failed_uuid)
    assert recovery(case)['state'] == 'blocked'
    assert recovery(case)['blocked_gpu_uuids'] == [failed_uuid]
