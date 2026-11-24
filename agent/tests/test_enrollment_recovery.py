"""Ordinary enrollment response loss and state failures, without network I/O."""
import json
import os
from dataclasses import replace

import pytest
import requests

from agent import state as state_store
from agent.client import RigPilotClient, ControllerRequestError, ProtocolError
from agent.config import AgentConfig
from agent.runtime import AgentRuntime
from agent.state import StateError, enrollment_pending_path, load_state, load_pending_enrollment

TOKEN = 'rpe_' + 'T' * 43
HOST = {'hostname':'first-host','os_family':'linux','platform':'Linux test'}
AGENT_ID = '12345678-1234-4234-8234-123456789abc'


def response(status, body):
    result=requests.Response();result.status_code=status
    result._content=json.dumps(body).encode();result.headers['Content-Type']='application/json'
    return result


class Controller:
    def __init__(self):
        self.records={};self.calls=[];self.lost_once=False;self.drop_next=False;self.supported=True
    def request(self, method, url, **kwargs):
        body=kwargs.get('json');self.calls.append((method,url,body))
        assert kwargs['allow_redirects'] is False
        if method=='GET' and url.endswith('/api/health'):
            return response(200,{'status':'ok',**({'enrollment_protocol':'rigpilot.enrollment.v2'} if self.supported else {})})
        assert method=='POST' and url.endswith('/api/agent/enroll')
        nonce=body.get('enrollment_request_id')
        if nonce is None:
            # Old one-time behavior reproduces the lost-response failure.
            if self.records:return response(401,{'detail':'Enrollment token already used'})
            result={'agent_id':AGENT_ID,'agent_secret':'old-server-generated-secret','heartbeat_interval':10}
            self.records['legacy']=(body,result)
        else:
            material={key:value for key,value in body.items() if key!='enrollment_token'}
            if nonce in self.records:
                first,result=self.records[nonce]
                if first!=material:return response(409,{'detail':'Request binding mismatch'})
            else:
                if body.get('enrollment_token') != TOKEN:
                    return response(401, {
                        'detail': 'A token is required for a new enrollment',
                        'code': 'enrollment_not_created',
                        'enrollment_request_id': nonce,
                    })
                result={'agent_id':AGENT_ID,'agent_secret':body['proposed_agent_secret'],'heartbeat_interval':10,'enrollment_request_id':nonce}
                self.records[nonce]=(material,result)
        if self.drop_next:
            self.drop_next=False;self.lost_once=True
            raise requests.ConnectionError('synthetic response loss')
        return response(201,result)


@pytest.fixture
def runtimes(tmp_path, monkeypatch):
    created=[];controller=Controller()
    monkeypatch.delenv('RIGPILOT_ENROLLMENT_TOKEN', raising=False)
    monkeypatch.delenv('RIGPILOT_REPLACEMENT_TOKEN', raising=False)
    cfg=AgentConfig(server_url='https://controller.example',state_file=tmp_path/'state.json',name='first-name')
    monkeypatch.setattr('agent.runtime.collect_host_data',lambda *_args,**_kwargs:dict(HOST))
    def new(config=cfg, host=None):
        runtime=AgentRuntime(config);created.append(runtime)
        session=requests.Session();session.request=controller.request
        runtime.client=RigPilotClient(config,session=session)
        runtime._host_data=lambda:dict(host or HOST)
        return runtime
    yield cfg,controller,new
    for runtime in created:assert runtime._host_data_sampler.close(timeout=1)


def test_lost_response_recovers_original_identity_after_restart_without_token(runtimes, monkeypatch):
    cfg,controller,new=runtimes;controller.drop_next=True
    with pytest.raises(ControllerRequestError):new().ensure_enrolled(TOKEN)
    pending=load_pending_enrollment(cfg.state_file,cfg.server_url)
    assert pending is not None
    assert TOKEN not in enrollment_pending_path(cfg.state_file).read_text()
    assert load_state(cfg.state_file) is None
    monkeypatch.setattr('agent.client.__version__','99.99.99')
    recovered=new(replace(cfg,name='changed-name'),{'hostname':'changed-host','os_family':'linux','platform':'changed'}).ensure_enrolled()
    assert len(controller.records)==1 and recovered.agent_id==AGENT_ID
    assert recovered.agent_secret==pending.proposed_agent_secret
    posts=[body for method,_url,body in controller.calls if method=='POST']
    assert len(posts)==2 and posts[0]['enrollment_request_id']==posts[1]['enrollment_request_id']
    assert {k:v for k,v in posts[0].items() if k!='enrollment_token'}==posts[1]
    assert load_state(cfg.state_file).agent_secret==recovered.agent_secret
    assert not enrollment_pending_path(cfg.state_file).exists()


def test_pending_write_failure_performs_no_network_request(runtimes, monkeypatch):
    _cfg,controller,new=runtimes
    def cannot_save(*_args,**_kwargs):raise StateError('synthetic disk failure')
    monkeypatch.setattr('agent.runtime.load_or_create_pending_enrollment',cannot_save)
    with pytest.raises(StateError):new().ensure_enrolled(TOKEN)
    assert controller.calls==[] and controller.records=={}


def test_primary_state_save_failure_replays_one_identity_after_restart(runtimes, monkeypatch):
    cfg,controller,new=runtimes
    def cannot_save(*_args,**_kwargs):raise StateError('synthetic state save failure')
    with monkeypatch.context() as m:
        m.setattr('agent.runtime.save_state',cannot_save)
        with pytest.raises(StateError):new().ensure_enrolled(TOKEN)
    pending=load_pending_enrollment(cfg.state_file,cfg.server_url);assert pending is not None
    saved=new().ensure_enrolled()
    assert len(controller.records)==1 and saved.agent_secret==pending.proposed_agent_secret
    assert load_state(cfg.state_file).agent_id==AGENT_ID
    assert not enrollment_pending_path(cfg.state_file).exists()


def test_pending_cleanup_failure_does_not_register_a_second_identity(runtimes, monkeypatch):
    cfg,controller,new=runtimes
    def cannot_remove(*_args,**_kwargs):raise StateError('synthetic cleanup failure')
    with monkeypatch.context() as m:
        m.setattr('agent.runtime.clear_pending_enrollment',cannot_remove)
        saved=new().ensure_enrolled(TOKEN)
        assert saved.agent_id==AGENT_ID and load_state(cfg.state_file) is not None
        assert enrollment_pending_path(cfg.state_file).exists()
        count=len(controller.calls)
        assert new().ensure_enrolled().agent_id==AGENT_ID
        assert len(controller.calls)==count
    assert new().ensure_enrolled().agent_id==AGENT_ID
    assert not enrollment_pending_path(cfg.state_file).exists() and len(controller.records)==1


def test_old_controller_is_rejected_before_post_without_legacy_fallback(runtimes):
    cfg,controller,new=runtimes;controller.supported=False
    with pytest.raises(ProtocolError,match='enrollment'):
        new().ensure_enrolled(TOKEN)
    assert [method for method,_url,_body in controller.calls]==['GET']
    assert controller.records=={}
    assert enrollment_pending_path(cfg.state_file).exists()


def test_pending_controller_or_token_change_does_not_submit_elsewhere(runtimes):
    cfg,controller,new=runtimes;controller.drop_next=True
    with pytest.raises(ControllerRequestError):new().ensure_enrolled(TOKEN)
    count=len(controller.calls)
    with pytest.raises(StateError):new(replace(cfg,server_url='https://other.example')).ensure_enrolled(TOKEN)
    with pytest.raises(StateError):new().ensure_enrolled('rpe_' + 'D' * 43)
    assert len(controller.calls)==count


@pytest.mark.parametrize('field,value',[('enrollment_request_id','f'*32),('agent_secret','rpa_'+'W'*48)])
def test_enrollment_response_must_match_published_nonce_and_secret(runtimes, field, value):
    cfg,controller,new=runtimes;original=controller.request
    def bad(method,url,**kwargs):
        result=original(method,url,**kwargs)
        if method=='POST' and result.status_code==201:
            body=result.json();body[field]=value;return response(201,body)
        return result
    controller.request=bad
    with pytest.raises(ProtocolError):new().ensure_enrolled(TOKEN)
    assert load_state(cfg.state_file) is None
    assert enrollment_pending_path(cfg.state_file).exists()


@pytest.mark.parametrize('restart_token', [TOKEN, None], ids=['with-token', 'without-token'])
def test_pending_directory_fsync_failure_blocks_every_attempt_until_durable(
    runtimes, monkeypatch, restart_token,
):
    cfg, controller, new = runtimes
    pending_path = enrollment_pending_path(cfg.state_file)
    sync_attempts = []

    def unavailable_directory_sync(path):
        sync_attempts.append(path)
        raise OSError('synthetic directory fsync failure')

    with monkeypatch.context() as patched:
        patched.setattr(state_store, '_fsync_directory', unavailable_directory_sync)
        with pytest.raises(StateError, match='durab'):
            new().ensure_enrolled(TOKEN)
        # Publication is visible, but its directory entry is not yet confirmed
        # durable. A restart must retain this exact identity and retry fsync.
        pending = load_pending_enrollment(cfg.state_file, cfg.server_url)
        assert pending is not None
        published = pending_path.read_bytes()
        assert controller.calls == [] and controller.records == {}
        assert load_state(cfg.state_file) is None
        before_retry = len(sync_attempts)

        with pytest.raises(StateError, match='durab'):
            new().ensure_enrolled(restart_token)
        assert len(sync_attempts) == before_retry + 1
        assert pending_path.read_bytes() == published
        assert load_pending_enrollment(cfg.state_file, cfg.server_url) == pending
        assert controller.calls == [] and controller.records == {}

    enrolled = new().ensure_enrolled(TOKEN)
    posts = [body for method, _url, body in controller.calls if method == 'POST']
    assert len(posts) == 1 and posts[0]['enrollment_request_id'] == pending.request_id
    assert enrolled.agent_secret == pending.proposed_agent_secret
    assert set(controller.records) == {pending.request_id}
    assert load_state(cfg.state_file).agent_id == AGENT_ID
    assert not pending_path.exists()


@pytest.mark.skipif(os.name == 'nt', reason='Windows save_state has no directory-fsync durability guarantee')
def test_visible_primary_state_must_be_durable_before_pending_cleanup(runtimes, monkeypatch):
    cfg, controller, new = runtimes
    pending_path = enrollment_pending_path(cfg.state_file)
    original_sync = state_store._fsync_directory
    cleanup_calls = []
    original_cleanup = state_store.clear_pending_enrollment

    def fail_after_primary_replace(path):
        if cfg.state_file.exists():
            raise OSError('synthetic primary directory fsync failure')
        original_sync(path)

    def observed_cleanup(state_path):
        cleanup_calls.append(state_path)
        original_cleanup(state_path)

    with monkeypatch.context() as patched:
        patched.setattr(state_store, '_fsync_directory', fail_after_primary_replace)
        patched.setattr('agent.runtime.clear_pending_enrollment', observed_cleanup)
        with pytest.raises(StateError):
            new().ensure_enrolled(TOKEN)
        # os.replace succeeded; only the final directory fsync failed.
        visible = load_state(cfg.state_file)
        pending = load_pending_enrollment(cfg.state_file, cfg.server_url)
        assert visible is not None and pending is not None
        assert visible.agent_id == AGENT_ID
        assert visible.agent_secret == pending.proposed_agent_secret
        published = pending_path.read_bytes()
        request_count = len(controller.calls)

        recovered = new().ensure_enrolled()
        assert recovered.agent_id == visible.agent_id
        assert recovered.agent_secret == visible.agent_secret
        assert len(controller.calls) == request_count
        assert cleanup_calls == []
        assert pending_path.read_bytes() == published
        assert load_pending_enrollment(cfg.state_file, cfg.server_url) == pending

    durable = new().ensure_enrolled()
    assert durable.agent_id == visible.agent_id
    assert durable.agent_secret == visible.agent_secret
    assert len(controller.calls) == request_count
    assert set(controller.records) == {pending.request_id}
    assert not pending_path.exists()


def test_exact_not_created_rejection_allows_a_new_valid_token(runtimes):
    cfg, controller, new = runtimes
    invalid_token = 'rpe_' + 'I' * 43

    with pytest.raises(ControllerRequestError) as rejected:
        new().ensure_enrolled(invalid_token)
    first_post = next(body for method, _url, body in controller.calls if method == 'POST')
    rejected_nonce = first_post['enrollment_request_id']
    assert rejected.value.http_status == 401
    assert rejected.value.rejected_enrollment_request_id == rejected_nonce
    assert controller.records == {} and load_state(cfg.state_file) is None
    assert not enrollment_pending_path(cfg.state_file).exists()

    enrolled = new().ensure_enrolled(TOKEN)
    posts = [body for method, _url, body in controller.calls if method == 'POST']
    assert len(posts) == 2 and posts[1]['enrollment_request_id'] != rejected_nonce
    assert posts[0]['enrollment_token'] == invalid_token
    assert posts[1]['enrollment_token'] == TOKEN
    assert set(controller.records) == {posts[1]['enrollment_request_id']}
    assert enrolled.agent_id == AGENT_ID
    assert load_state(cfg.state_file).agent_secret == enrolled.agent_secret
    assert not enrollment_pending_path(cfg.state_file).exists()


@pytest.mark.parametrize(
    'status, code, matching_nonce, include_token',
    [
        (401, None, True, True),
        (401, 'another_error', True, True),
        (401, 'enrollment_not_created', False, True),
        (403, 'enrollment_not_created', True, True),
        (401, 'enrollment_not_created', True, False),
    ],
    ids=['generic-401', 'wrong-code', 'wrong-nonce', 'forbidden-403', 'tokenless'],
)
def test_other_rejections_preserve_pending_identity(
    runtimes, monkeypatch, status, code, matching_nonce, include_token,
):
    cfg, controller, new = runtimes
    pending = state_store.load_or_create_pending_enrollment(
        cfg.state_file, cfg.server_url, TOKEN, RigPilotClient.enrollment_metadata(cfg, HOST),
    )
    pending_path = enrollment_pending_path(cfg.state_file)
    published = pending_path.read_bytes()
    original_request = controller.request

    def reject(method, url, **kwargs):
        if method != 'POST':
            return original_request(method, url, **kwargs)
        assert kwargs['allow_redirects'] is False
        body = kwargs['json']
        controller.calls.append((method, url, body))
        payload = {'detail': 'synthetic enrollment rejection'}
        if code is not None:
            payload['code'] = code
            payload['enrollment_request_id'] = (
                body['enrollment_request_id'] if matching_nonce
                else ('0' if pending.request_id[0] != '0' else '1') + pending.request_id[1:]
            )
        return response(status, payload)

    monkeypatch.setattr(controller, 'request', reject)
    with pytest.raises(ControllerRequestError):
        new().ensure_enrolled(TOKEN if include_token else None)
    assert pending_path.read_bytes() == published
    assert load_pending_enrollment(cfg.state_file, cfg.server_url) == pending
    assert load_state(cfg.state_file) is None and controller.records == {}
    posts = [body for method, _url, body in controller.calls if method == 'POST']
    assert len(posts) == 1 and posts[0]['enrollment_request_id'] == pending.request_id
    assert ('enrollment_token' in posts[0]) is include_token


def test_exact_rejection_does_not_clear_a_changed_pending_object(runtimes, monkeypatch):
    cfg, controller, new = runtimes
    pending = state_store.load_or_create_pending_enrollment(
        cfg.state_file, cfg.server_url, TOKEN, RigPilotClient.enrollment_metadata(cfg, HOST),
    )
    pending_path = enrollment_pending_path(cfg.state_file)
    original_request = controller.request

    def reject_after_change(method, url, **kwargs):
        if method != 'POST':
            return original_request(method, url, **kwargs)
        assert kwargs['allow_redirects'] is False
        body = kwargs['json']
        controller.calls.append((method, url, body))
        changed = json.loads(pending_path.read_text())
        changed['metadata']['hostname'] = 'different-pending-host'
        pending_path.write_text(json.dumps(changed))
        return response(401, {
            'detail': 'synthetic enrollment rejection',
            'code': 'enrollment_not_created',
            'enrollment_request_id': body['enrollment_request_id'],
        })

    monkeypatch.setattr(controller, 'request', reject_after_change)
    with pytest.raises(ControllerRequestError):
        new().ensure_enrolled(TOKEN)
    remaining = load_pending_enrollment(cfg.state_file, cfg.server_url)
    assert remaining is not None and remaining.request_id == pending.request_id
    assert remaining.proposed_agent_secret == pending.proposed_agent_secret
    assert remaining.metadata['hostname'] == 'different-pending-host'
    assert load_state(cfg.state_file) is None and controller.records == {}


@pytest.mark.parametrize(
    'token', ['rpe_short', 'T' * 257, ' ' * 30],
    ids=['too-short', 'too-long', 'whitespace-only'],
)
def test_invalid_initial_token_creates_no_pending_and_sends_no_http(runtimes, token):
    cfg, controller, new = runtimes
    with pytest.raises(ProtocolError, match='20.*256'):
        new().ensure_enrolled(token)
    assert controller.calls == [] and controller.records == {}
    assert load_state(cfg.state_file) is None
    assert not enrollment_pending_path(cfg.state_file).exists()


@pytest.mark.parametrize("failure, expected_code", [
    ("not_created", 10), ("generic_401", 2),
    ("lost_response", 2), ("cleanup_failure", 2),
])
def test_cli_unlocks_outer_bootstrap_only_after_definitive_rejection_and_cleanup(
    runtimes, monkeypatch, failure, expected_code,
):
    import agent.__main__ as cli

    cfg, controller, new = runtimes
    token = "rpe_" + "I" * 43
    if failure == "lost_response":
        token = TOKEN
        controller.drop_next = True
    elif failure == "generic_401":
        original = controller.request

        def generic_rejection(method, url, **kwargs):
            if method == "POST":
                return response(401, {"detail": "synthetic generic rejection"})
            return original(method, url, **kwargs)

        monkeypatch.setattr(controller, "request", generic_rejection)
    elif failure == "cleanup_failure":
        def failed_cleanup(*_args, **_kwargs):
            raise StateError("synthetic cleanup failure")

        monkeypatch.setattr("agent.runtime.clear_pending_enrollment", failed_cleanup)
    runtime = new()
    monkeypatch.setattr(runtime._host_data_sampler, "wait_for_first", lambda _timeout: True)
    monkeypatch.setattr(runtime, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(cli, "configure_logging", lambda *_a: None)
    monkeypatch.setattr(cli, "AgentRuntime", lambda _config: runtime)
    assert cli.main(["--config", "synthetic-config.json", "--once", "--enrollment-token", token]) == expected_code
    assert enrollment_pending_path(cfg.state_file).exists() is (expected_code != 10)
    assert load_state(cfg.state_file) is None
