"""Exercise actual history reads across aliases, sample replacement and storage."""
import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest
from test_059_backend import app_client, auth, enroll, login, sample, telemetry_metrics


def rated(coin, algorithm, value, unit='H/s', role='primary'):
    return {**sample(role, coin, algorithm, value), 'unit':unit}


def test_history_coin_sum_preserves_latest_snapshot_units_gaps_and_raw_history(app_client):
    client, _ = app_client; token=login(client)
    a,_=enroll(client,token,'alias-a'); b,_=enroll(client,token,'alias-b')
    epoch=int((datetime.now(timezone.utc)-timedelta(hours=1)).timestamp())
    base=datetime.fromtimestamp(epoch-epoch%300,timezone.utc)
    latest=[rated('PEARL','pearlhash',2.50,'PH/s'),rated('pearl','pearl',180,'TH/s','secondary')]
    rows=[(a,base+timedelta(seconds=10),[rated('PEARL','pearl',99,'PH/s')]),
        (a,base+timedelta(seconds=20),latest),
        (b,base+timedelta(seconds=15),[rated('PEARL','pearl',852.35,'TH/s')]),
        (a,base+timedelta(minutes=5,seconds=10),[rated('PEARL','pearlhash',0)]),
        (b,base+timedelta(minutes=5,seconds=10),[rated('PEARL','pearl',40,'TH/s')]),
        (b,base+timedelta(minutes=5,seconds=20),[]),
        (a,base+timedelta(minutes=15,seconds=10),[rated('PEARL','pearl',300,'TH/s')])]
    with client.app.state.db.connection() as db:
        for agent,stamp,samples in rows:
            db.execute('INSERT INTO telemetry(agent_id,recorded_at,metrics_json) VALUES(?,?,?)',
                (agent,stamp.isoformat(),telemetry_metrics(samples)))
        # Compact and legacy overlap represents the same snapshot, not extra work.
        db.execute('INSERT INTO hashrate_history_buckets(agent_id,bucket_start,recorded_at,samples_json) VALUES(?,?,?,?)',
            (a,base.isoformat(),(base+timedelta(seconds=20)).isoformat(),json.dumps(latest)))
        before={table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
            for table in ['telemetry','hashrate_history_buckets','device_work_state','device_work_buckets']}
    response=client.get('/api/hashrate-history?hours=24&bucket_minutes=5',headers=auth(token))
    assert response.status_code==200,response.text
    history=response.json()
    if os.getenv('RIGPILOT_CHART_API_EVIDENCE'):
        Path(os.environ['RIGPILOT_CHART_API_EVIDENCE']).write_text(json.dumps(history))
    assert len(history['series'])==1
    assert history['group_by']=='coin'
    series=history['series'][0]
    assert series['coin']=='PEARL' and series['unit']=='H/s'
    assert series['algorithms']==['pearl','pearlhash']
    assert [p['value'] for p in series['points']]==pytest.approx([3.53235e15,0,300e12])
    assert [p['agent_count'] for p in series['points']]==[2,1,1]
    assert [datetime.fromisoformat(p['timestamp']) for p in series['points']]==[
        base,base+timedelta(minutes=5),base+timedelta(minutes=15)]
    with client.app.state.db.connection() as db:
        assert before=={table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in before}


def test_unknown_coin_does_not_merge_unidentified_devices_or_algorithms(app_client):
    client,_=app_client;token=login(client);a,_=enroll(client,token,'unknown-a');b,_=enroll(client,token,'unknown-b')
    stamp=(datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
    with client.app.state.db.connection() as db:
        for agent,samples in [(a,[rated('', 'pearl',100),rated('', 'pearlhash',200),rated('PEARL','pearl',300)]),
                               (b,[rated(None,'pearl',400)])]:
            db.execute('INSERT INTO telemetry(agent_id,recorded_at,metrics_json) VALUES(?,?,?)',(agent,stamp,telemetry_metrics(samples)))
    series=client.get('/api/hashrate-history',headers=auth(token)).json()['series']
    unknown=[s for s in series if s['coin']=='UNKNOWN']
    assert len(unknown)==3
    assert sorted(s['points'][0]['value'] for s in unknown)==[100,200,400]
    assert all(s['aggregation']=='unknown_source' for s in unknown)
    assert next(s for s in series if s['coin']=='PEARL')['points'][0]['value']==300


def test_agent_filter_scopes_compact_and_legacy_history(app_client):
    client, _ = app_client
    token = login(client)
    first, _ = enroll(client, token, 'history-first')
    second, _ = enroll(client, token, 'history-second')
    epoch = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
    base = datetime.fromtimestamp(epoch - epoch % 300, timezone.utc)
    compact = base + timedelta(minutes=5)
    with client.app.state.db.connection() as db:
        for agent, value in [(first, 100), (second, 200)]:
            stamp = (base + timedelta(seconds=10)).isoformat()
            db.execute('INSERT INTO telemetry(agent_id,recorded_at,metrics_json) VALUES(?,?,?)',
                       (agent, stamp, telemetry_metrics([rated('PEARL', 'pearlhash', value)])))
        for agent, value in [(first, 300), (second, 400)]:
            db.execute('INSERT INTO hashrate_history_buckets(agent_id,bucket_start,recorded_at,samples_json) VALUES(?,?,?,?)',
                       (agent, compact.isoformat(), (compact + timedelta(seconds=20)).isoformat(),
                        json.dumps([rated('PEARL', 'pearlhash', value)])))
    first_result = client.get(f'/api/hashrate-history?agent_id={first}', headers=auth(token))
    second_result = client.get(f'/api/hashrate-history?agent_id={second}', headers=auth(token))
    fleet_result = client.get('/api/hashrate-history', headers=auth(token))
    assert first_result.status_code == second_result.status_code == fleet_result.status_code == 200
    assert first_result.json()['agent_id'] == first
    assert second_result.json()['agent_id'] == second
    assert 'agent_id' not in fleet_result.json()
    assert [point['value'] for point in first_result.json()['series'][0]['points']] == pytest.approx([100, 300])
    assert [point['value'] for point in second_result.json()['series'][0]['points']] == pytest.approx([200, 400])
    assert [point['value'] for point in fleet_result.json()['series'][0]['points']] == pytest.approx([300, 700])
    assert client.get('/api/hashrate-history?agent_id=unknown', headers=auth(token)).json()['series'] == []
