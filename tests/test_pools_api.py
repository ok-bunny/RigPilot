"""Saved pool records copy into flights explicitly and keep viewer secrets hidden."""
import json

from test_059_backend import app_client, auth, create_user, login


def test_pool_crud_role_redaction_and_existing_flight_snapshot(app_client):
    client, _ = app_client
    admin = login(client)
    operator = create_user(client, admin, 'pool-operator', 'operator')
    viewer = create_user(client, admin, 'pool-viewer', 'viewer')
    payload = {
        'name': 'Main PEARL SSL', 'coin': 'pearl',
        'pool_url': 'stratum+ssl://pool.example.test:443',
        'pool_password': 'worker-tag', 'note': 'saved template',
    }
    denied = client.post('/api/pools', headers=auth(viewer), json=payload)
    assert denied.status_code == 403
    created = client.post('/api/pools', headers=auth(operator), json=payload)
    assert created.status_code == 201, created.text
    pool = created.json()
    assert pool['coin'] == 'PEARL'
    assert pool['protocol'] == 'stratum+ssl' and pool['tls'] is True
    assert pool['pool_password'] == 'worker-tag'
    listed = client.get('/api/pools', headers=auth(viewer))
    assert listed.status_code == 200
    assert listed.json()[0]['pool_password'] is None
    assert listed.json()[0]['pool_password_configured'] is True
    assert listed.json()[0]['pool_url'] == payload['pool_url']

    wallet = client.post('/api/wallets', headers=auth(operator), json={
        'name': 'PEARL wallet', 'coin': 'PEARL', 'address': 'prl1-public-pool-test-address',
    })
    assert wallet.status_code == 201, wallet.text
    flight = client.post('/api/flight-sheets', headers=auth(operator), json={
        'name': 'Snapshot sheet', 'coin': 'PEARL', 'wallet_id': wallet.json()['id'],
        'pool_url': pool['pool_url'], 'pool_password': pool['pool_password'],
    })
    assert flight.status_code == 201, flight.text
    original = flight.json()
    changed = client.put(f"/api/pools/{pool['id']}", headers=auth(operator), json={
        **payload, 'name': 'Main PEARL TCP',
        'pool_url': 'stratum+tcp://pool.example.test:3333', 'pool_password': '',
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()['protocol'] == 'stratum+tcp'
    assert changed.json()['tls'] is False
    assert changed.json()['pool_password_configured'] is False
    assert set(changed.json()['changed_fields']) == {'name', 'pool_url', 'pool_password'}
    unchanged = client.get('/api/flight-sheets', headers=auth(operator)).json()
    assert unchanged[0]['id'] == original['id']
    assert unchanged[0]['pool_url'] == original['pool_url']
    assert unchanged[0]['pool_password'] == original['pool_password']
    with client.app.state.db.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM configuration_refreshes').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM commands').fetchone()[0] == 0
        pool_audit = [row[0] for row in db.execute(
            "SELECT details_json FROM audit_logs WHERE target_type='pool'")]
        assert pool_audit and all('worker-tag' not in value for value in pool_audit)
        assert all('pool_password' not in json.loads(value) for value in pool_audit)
    assert client.delete(f"/api/pools/{pool['id']}", headers=auth(operator)).status_code == 403
    with client.app.state.db.connection() as db:
        creator_id = db.execute("SELECT id FROM users WHERE username='pool-operator'").fetchone()[0]
    creator_deleted = client.delete(f'/api/users/{creator_id}', headers=auth(admin))
    assert creator_deleted.status_code == 200, creator_deleted.text
    assert client.get('/api/pools', headers=auth(admin)).json()[0]['created_by'] == 'admin'
    deleted = client.delete(f"/api/pools/{pool['id']}", headers=auth(admin))
    assert deleted.status_code == 200
    assert client.get('/api/pools', headers=auth(admin)).json() == []
    assert client.get('/api/flight-sheets', headers=auth(admin)).json()[0]['pool_url'] == original['pool_url']


def test_pool_url_and_password_validation_reuses_stratum_rules(app_client):
    client, _ = app_client
    admin = login(client)
    base = {'name': 'Test', 'coin': 'PEARL', 'pool_password': ''}
    for url in ('stratum+ssl://user:secret@pool.example.test:443',
                'stratum+ssl://pool.example.test',
                'stratum+tcp://pool.example.test:3333/path',
                'https://pool.example.test:443',
                'stratum+tcp://pool.example.test:3333\nInjected'):
        response = client.post('/api/pools', headers=auth(admin), json={**base, 'pool_url': url})
        assert response.status_code == 422, (url, response.text)
    bad_password = client.post('/api/pools', headers=auth(admin), json={
        **base, 'pool_url': 'stratum+tcp://pool.example.test:3333',
        'pool_password': 'bad\nvalue',
    })
    assert bad_password.status_code == 422
    pearl = client.post('/api/pools', headers=auth(admin), json={
        **base, 'pool_url': 'pearl://pool.example.test:3333',
    })
    assert pearl.status_code == 201, pearl.text
    assert pearl.json()['protocol'] == 'pearl'
