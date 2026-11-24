"""Device log reads use exact identities and retain global audit compatibility."""
from datetime import datetime, timedelta, timezone

from server.database import Database
from test_059_backend import app_client, auth, create_user, enroll, login


def test_device_filtered_commands_and_audit_do_not_cross_same_name_or_global_records(app_client):
    client, _ = app_client
    admin = login(client)
    viewer = create_user(client, admin, 'device-log-viewer', 'viewer')
    first, _ = enroll(client, admin, 'same-display-name')
    second, _ = enroll(client, admin, 'same-display-name')
    assert first != second
    now = datetime.now(timezone.utc)
    with client.app.state.db.connection() as db:
        admin_id = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
        def command(command_id, agent_id, age):
            created = (now - timedelta(minutes=age)).isoformat()
            db.execute(
                """INSERT INTO commands
                   (id,agent_id,action,payload_json,status,created_by,created_at,expires_at,is_transient)
                   VALUES(?,?,?,?,?,?,?,?,0)""",
                (command_id, agent_id, 'collect_diagnostics', '{}', 'succeeded', admin_id,
                 created, (now + timedelta(days=1)).isoformat()),
            )
        command('first-old-command', first, 200)
        for index in range(120):
            command(f'second-command-{index:03d}', second, 120 - index)
        Database.audit(db, actor='admin', action='agent_note_changed', target_type='agent',
                       target_id=first, details={'marker': 'first-only'})
        Database.audit(db, actor='admin', action='agent_note_changed', target_type='agent',
                       target_id=second, details={'marker': 'second-only'})
        Database.audit(db, actor='admin', action='user_created', target_type='user',
                       target_id='global-user', details={'agent_id': first, 'marker': 'global-only'})
        # The command row wins over conflicting JSON metadata.
        Database.audit(db, actor='agent', action='command_succeeded', target_type='command',
                       target_id='first-old-command', details={'agent_id': second, 'marker': 'first-command'})
        # Old pruned command/session rows can still use their exact recorded agent_id.
        Database.audit(db, actor='agent', action='command_expired', target_type='command',
                       target_id='pruned-first-command', details={'agent_id': first, 'marker': 'pruned-first'})
        Database.audit(db, actor='admin', action='remote_session_closed', target_type='remote_session',
                       target_id='pruned-first-session', details={'agent_id': first, 'marker': 'first-session'})
        Database.audit(db, actor='admin', action='flight_sheet_applied', target_type='flight_sheet',
                       target_id='shared-sheet', details={'agent_id': first, 'marker': 'first-flight'})

    # A global 100-row window no longer contains the older first-device command.
    global_commands = client.get('/api/commands?limit=100', headers=auth(admin))
    assert global_commands.status_code == 200
    assert 'first-old-command' not in {item['id'] for item in global_commands.json()}
    device_commands = client.get(f'/api/commands?agent_id={first}&limit=100&include_all_active=true',
                                 headers=auth(admin))
    assert device_commands.status_code == 200
    assert {item['id'] for item in device_commands.json()} == {'first-old-command'}

    response = client.get(f'/api/audit?agent_id={first}&limit=100', headers=auth(admin))
    assert response.status_code == 200, response.text
    rows = response.json()
    markers = {item['details'].get('marker') for item in rows}
    assert {'first-only', 'first-command', 'pruned-first', 'first-session', 'first-flight'} <= markers
    assert 'second-only' not in markers and 'global-only' not in markers
    assert all(item['related_agent_id'] == first for item in rows)
    second_rows = client.get(f'/api/audit?agent_id={second}&limit=100', headers=auth(admin)).json()
    second_markers = {item['details'].get('marker') for item in second_rows}
    assert 'second-only' in second_markers and 'first-command' not in second_markers
    assert 'first-only' not in second_markers

    viewer_rows = client.get(f'/api/audit?agent_id={first}', headers=auth(viewer))
    assert viewer_rows.status_code == 200
    assert viewer_rows.json() and all(item['details'] == {} for item in viewer_rows.json())
    global_audit = client.get('/api/audit?limit=500', headers=auth(admin))
    assert global_audit.status_code == 200
    assert any(item['details'].get('marker') == 'global-only' for item in global_audit.json())
    assert all('related_agent_id' not in item for item in global_audit.json())
