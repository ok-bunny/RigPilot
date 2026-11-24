"""Ordinary bootstrap recovery against real SQLite and ASGI; no live Agent."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from server.auth import hash_api_secret
from server.config import Settings
from server.database import Database
from server.main import create_app
from server.schemas import AgentEnrollRequest
from test_device_replacement_and_settlement import client, _admin_headers, _replacement_body

ENDPOINT='/api/agent/enroll'

def issue(client, headers=None):
    response=client.post('/api/enrollment-tokens',headers=headers or _admin_headers(client),json={'label':'bootstrap-recovery','ttl_minutes':5})
    assert response.status_code==200,response.text
    return response.json()

def payload(token):
    return {'enrollment_token':token,'enrollment_request_id':uuid.uuid4().hex,
            'proposed_agent_secret':'rpa_'+secrets.token_urlsafe(36),'name':'new-device',
            'hostname':'first-host','os_family':'linux','platform':'synthetic-linux','agent_version':'0.8.18'}

def rows(client, table):
    with client.app.state.db.connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

def snapshot(client):
    return {table:rows(client,table) for table in ('agents','enrollment_tokens','agent_enrollment_attempts','audit_logs')}

def enroll(client,body):
    response=client.post(ENDPOINT,json=body)
    assert response.status_code==201,response.text
    return response


def test_feature_negotiation_and_legacy_request_remain_one_time(client):
    assert client.get('/api/health').json()['enrollment_protocol']=='rigpilot.enrollment.v2'
    token=issue(client)['token'];body={'enrollment_token':token,'hostname':'legacy','os_family':'linux'}
    response=enroll(client,body)
    assert set(response.json())=={'agent_id','agent_secret','heartbeat_interval'}
    assert client.post(ENDPOINT,json=body).status_code==401
    assert rows(client,'agent_enrollment_attempts')==[]
    assert client.post(ENDPOINT,json=payload(token)).status_code==401
    assert len(rows(client,'agents'))==1

@pytest.mark.parametrize('token_mode',['same','missing','empty','null'])
def test_known_attempt_replays_same_identity_without_side_effects_after_response_loss(client,token_mode):
    body=payload(issue(client)['token']);first=enroll(client,body).json();before=snapshot(client)
    retry=dict(body)
    if token_mode=='missing':retry.pop('enrollment_token')
    elif token_mode=='empty':retry['enrollment_token']=''
    elif token_mode=='null':retry['enrollment_token']=None
    response=enroll(client,retry)
    assert response.json()==first
    assert response.json()['agent_secret']==body['proposed_agent_secret']
    assert response.json()['enrollment_request_id']==body['enrollment_request_id']
    assert snapshot(client)==before
    assert 'no-store' in response.headers['cache-control']
    assert len(rows(client,'agents'))==len(rows(client,'agent_enrollment_attempts'))==1


def test_expired_issuance_still_allows_known_proof_but_unknown_attempt_cannot_create(client):
    body=payload(issue(client)['token']);first=enroll(client,body).json()
    expired=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    with client.app.state.db.connection() as db:db.execute('UPDATE enrollment_tokens SET expires_at=?',(expired,))
    assert enroll(client,body).json()==first
    assert client.post(ENDPOINT,json={**body,'enrollment_request_id':uuid.uuid4().hex}).status_code==401
    assert client.post(ENDPOINT,json={k:v for k,v in payload(None).items() if k!='enrollment_token'}).status_code==401
    assert len(rows(client,'agents'))==1

@pytest.mark.parametrize('change',[{'hostname':'changed'},{'name':'changed'},{'os_family':'windows'},{'platform':'changed'},{'agent_version':'changed'}])
def test_frozen_metadata_mismatch_cannot_rewrite_an_existing_identity(client,change):
    body=payload(issue(client)['token']);enroll(client,body);before=snapshot(client)
    response=client.post(ENDPOINT,json={**body,**change})
    assert response.status_code==409,response.text;assert snapshot(client)==before

@pytest.mark.parametrize('proof',['request','secret','token'])
def test_wrong_recovery_proof_cannot_recover_or_change_a_device(client,proof):
    body=payload(issue(client)['token']);enroll(client,body)
    retry=dict(body)
    if proof=='request':retry['enrollment_request_id']=uuid.uuid4().hex
    elif proof=='secret':retry['proposed_agent_secret']='rpa_'+secrets.token_urlsafe(36)
    else:retry['enrollment_token']=issue(client)['token']
    before=snapshot(client);response=client.post(ENDPOINT,json=retry)
    assert response.status_code==401,response.text;assert snapshot(client)==before


def test_same_secret_on_a_different_token_is_rejected_without_consuming_that_token(client):
    body=payload(issue(client)['token']);enroll(client,body)
    second=issue(client);before=snapshot(client)
    retry={**body,'enrollment_token':second['token'],'enrollment_request_id':uuid.uuid4().hex}
    assert client.post(ENDPOINT,json=retry).status_code==409
    assert snapshot(client)==before
    retry['proposed_agent_secret']='rpa_'+secrets.token_urlsafe(36)
    assert enroll(client,retry).json()['agent_id']!=rows(client,'agents')[0]['id']

@pytest.mark.parametrize('lifecycle',['disabled','deleted','replaced'])
def test_admin_lifecycle_changes_revoke_pending_recovery_without_resurrection(client,lifecycle):
    headers=_admin_headers(client);body=payload(issue(client,headers)['token']);first=enroll(client,body).json();aid=first['agent_id']
    if lifecycle=='disabled':
        changed=client.patch('/api/agents/'+aid,headers=headers,json={'disabled':True})
        assert changed.status_code==200,changed.text
    elif lifecycle=='deleted':
        changed=client.delete('/api/agents/'+aid,headers=headers)
        assert changed.status_code==200,changed.text
        assert rows(client,'agent_enrollment_attempts')[0]['agent_id'] is None
    else:
        replacement=client.post(f'/api/agents/{aid}/replacement-token',headers=headers,json={'confirm_name':'new-device'})
        assert replacement.status_code==201,replacement.text
        new_secret='rpa_'+secrets.token_urlsafe(36)
        changed=client.post('/api/agent/replace',json=_replacement_body(replacement.json()['token'],uuid.uuid4().hex,new_secret))
        assert changed.status_code==201,changed.text
    before=snapshot(client);retry={k:v for k,v in body.items() if k!='enrollment_token'}
    assert client.post(ENDPOINT,json=retry).status_code==409
    assert snapshot(client)==before


def test_enrollment_and_replacement_tokens_are_not_interchangeable(client):
    headers=_admin_headers(client);body=payload(issue(client,headers)['token']);first=enroll(client,body).json()
    replacement=client.post(f"/api/agents/{first['agent_id']}/replacement-token",headers=headers,json={'confirm_name':'new-device'}).json()
    before=snapshot(client)
    assert client.post(ENDPOINT,json=payload(replacement['token'])).status_code==401
    assert snapshot(client)==before
    with client.app.state.db.connection() as db:assert db.execute('SELECT used_at FROM agent_replacement_tokens').fetchone()[0] is None

@pytest.mark.parametrize('trigger',['receipt','audit'])
def test_first_claim_rolls_back_token_agent_receipt_and_audit_then_retries(client,trigger):
    body=payload(issue(client)['token']);before=snapshot(client)
    sql=("CREATE TRIGGER enrollment_failure BEFORE INSERT ON agent_enrollment_attempts BEGIN SELECT RAISE(ABORT,'synthetic receipt failure'); END" if trigger=='receipt' else
         "CREATE TRIGGER enrollment_failure BEFORE INSERT ON audit_logs WHEN NEW.action='agent_enrolled' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END")
    with client.app.state.db.connection() as db:db.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):client.post(ENDPOINT,json=body)
    assert snapshot(client)==before
    with client.app.state.db.connection() as db:db.execute('DROP TRIGGER enrollment_failure')
    enroll(client,body);assert len(rows(client,'agents'))==1


def test_concurrent_same_request_claims_one_device_and_audits_once(client):
    body=payload(issue(client)['token'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(ENDPOINT,json=body),range(2)))
    assert [r.status_code for r in responses]==[201,201]
    assert responses[0].json()==responses[1].json()
    assert len(rows(client,'agents'))==len(rows(client,'agent_enrollment_attempts'))==1
    assert sum(row['action']=='agent_enrolled' for row in rows(client,'audit_logs'))==1


def test_controller_restart_recovers_client_pending_after_first_state_save_failure(tmp_path):
    settings=Settings(data_dir=tmp_path,database_path=tmp_path/'recovery.db',secret_key='synthetic-enrollment-restart-key',admin_username='admin',admin_password='correct-horse-battery-staple')
    with TestClient(create_app(settings)) as first:
        body=payload(issue(first)['token']);answer=enroll(first,body).json()
        # Simulate a client unable to commit its main state: only pre-POST
        # pending proof and the original metadata survive; original token does not.
        pending={k:v for k,v in body.items() if k!='enrollment_token'}
    with TestClient(create_app(settings)) as restarted:
        assert enroll(restarted,pending).json()==answer
        assert len(rows(restarted,'agents'))==1


def test_maintenance_prunes_receipt_with_token_and_never_recreates_unknown_identity(client):
    body=payload(issue(client)['token']);enroll(client,body)
    old=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
    with client.app.state.db.connection() as db:db.execute('UPDATE enrollment_tokens SET expires_at=?,used_at=?',(old,old))
    assert client.app.state.db.prune_enrollment_tokens(30)==1
    assert rows(client,'agent_enrollment_attempts')==[]
    assert client.post(ENDPOINT,json={k:v for k,v in body.items() if k!='enrollment_token'}).status_code==401
    assert len(rows(client,'agents'))==1


def test_additive_table_creation_preserves_legacy_agents_and_used_tokens(client):
    token=issue(client)['token'];enroll(client,{'enrollment_token':token,'hostname':'legacy','os_family':'linux'})
    agents=rows(client,'agents');tokens=rows(client,'enrollment_tokens')
    with client.app.state.db.connection() as db:db.execute('DROP TABLE agent_enrollment_attempts')
    client.app.state.db.initialize('admin',None)
    assert rows(client,'agents')==agents and rows(client,'enrollment_tokens')==tokens
    assert rows(client,'agent_enrollment_attempts')==[]

@pytest.mark.parametrize('change',[{'enrollment_request_id':None},{'proposed_agent_secret':None},{'enrollment_request_id':'bad'},{'proposed_agent_secret':'private-malformed-secret-marker'}])
def test_invalid_partial_proof_is_redacted_and_does_not_consume_token(client,caplog,change):
    body=payload(issue(client)['token']);before=snapshot(client);response=client.post(ENDPOINT,json={**body,**change})
    assert response.status_code==422,response.text
    assert body['enrollment_token'] not in response.text and body['proposed_agent_secret'] not in response.text
    assert 'private-malformed-secret-marker' not in response.text
    assert '"input"' not in response.text and '"ctx"' not in response.text
    assert snapshot(client)==before
    assert body['enrollment_token'] not in caplog.text and body['proposed_agent_secret'] not in caplog.text


def test_receipt_database_and_audits_contain_hashes_not_bootstrap_secrets(client,caplog):
    body=payload(issue(client)['token']);enroll(client,body)
    with client.app.state.db.connection() as db:
        dump='\n'.join(db.iterdump());receipt=dict(db.execute('SELECT * FROM agent_enrollment_attempts').fetchone())
    for value in (body['enrollment_token'],body['proposed_agent_secret'],body['enrollment_request_id']):
        assert value not in dump and value not in caplog.text and value not in repr(AgentEnrollRequest.model_validate(body))
    assert receipt['request_id_hash']==hash_api_secret(body['enrollment_request_id'])
    assert receipt['secret_hash']==hash_api_secret(body['proposed_agent_secret'])


def test_first_claim_rechecks_expiry_after_acquiring_the_write_transaction(client, monkeypatch):
    from contextlib import contextmanager
    from server import main as server_main
    body=payload(issue(client)['token'])
    before=snapshot(client)
    inside=[False]
    original=client.app.state.db.connection
    @contextmanager
    def observed_connection():
        with original() as db:
            db.set_trace_callback(lambda sql: inside.__setitem__(0,True) if sql == 'BEGIN IMMEDIATE' else None)
            yield db
    monkeypatch.setattr(client.app.state.db,'connection',observed_connection)
    monkeypatch.setattr(server_main,'utcnow',lambda: '2999-01-01T00:00:00+00:00' if inside[0] else '2000-01-01T00:00:00+00:00')
    response=client.post(ENDPOINT,json=body)
    assert response.status_code==401,response.text
    assert inside[0] is True and snapshot(client)==before


@pytest.mark.parametrize('token_kind',['unknown','expired','used_by_other_attempt'])
def test_unclaimed_attempt_gets_bound_rejection_code_and_can_correct_token(client,token_kind):
    issued=issue(client)
    rejected_token=issued['token']
    if token_kind=='unknown':rejected_token='rpe_'+secrets.token_urlsafe(32)
    elif token_kind=='expired':
        with client.app.state.db.connection() as db:db.execute("UPDATE enrollment_tokens SET expires_at=? WHERE id=?",((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),issued['id']))
    else:enroll(client,payload(rejected_token))
    body=payload(rejected_token);before=snapshot(client)
    response=client.post(ENDPOINT,json=body)
    assert response.status_code==401,response.text
    assert response.json()=={'detail':'Invalid or expired enrollment token','code':'enrollment_not_created','enrollment_request_id':body['enrollment_request_id']}
    assert body['enrollment_token'] not in response.text and body['proposed_agent_secret'] not in response.text
    assert snapshot(client)==before
    corrected=payload(issue(client)['token']);enroll(client,corrected)
    assert len(rows(client,'agents'))==len(before['agents'])+1


def test_rejection_cleanup_code_is_never_given_for_bound_or_tokenless_pending(client):
    issued=issue(client);body=payload(issued['token']);answer=enroll(client,body).json()
    attempts=[
        {**body,'proposed_agent_secret':'rpa_'+secrets.token_urlsafe(36)},
        {**body,'enrollment_request_id':uuid.uuid4().hex},
        {**body,'enrollment_token':'rpe_'+secrets.token_urlsafe(32)},
        {**body,'hostname':'drift'},
        {k:v for k,v in payload(None).items() if k!='enrollment_token'},
    ]
    for attempt in attempts:
        response=client.post(ENDPOINT,json=attempt)
        assert response.status_code in (401,409) and 'code' not in response.json()
    with client.app.state.db.connection() as db:db.execute("UPDATE agents SET disabled_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),answer['agent_id']))
    response=client.post(ENDPOINT,json=body)
    assert response.status_code==409 and 'code' not in response.json()
