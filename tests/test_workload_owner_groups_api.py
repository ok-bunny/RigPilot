"""Authenticated owner-group persistence against isolated ASGI apps and SQLite."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


OWNER_PATH = "/api/workload-owner-groups"
PASSWORD = "task34-synthetic-password-long-enough"
AGENT_A = "00000000-0000-4000-8000-000000000001"
AGENT_B = "00000000-0000-4000-8000-000000000002"
UNKNOWN_AGENT = "00000000-0000-4000-8000-000000000099"
GROUP_A = "00000000-0000-4000-8000-000000000101"
GROUP_B = "00000000-0000-4000-8000-000000000102"


@pytest.fixture()
def owner_client(tmp_path: Path, monkeypatch):
    # The module-level app, if imported here for the first time, is isolated too.
    monkeypatch.setenv("RIGPILOT_DATA_DIR", str(tmp_path / "import-data"))
    monkeypatch.setenv("RIGPILOT_DATABASE", str(tmp_path / "import-data" / "controller.db"))
    monkeypatch.setenv("RIGPILOT_SECRET_KEY", "task34-synthetic-import-secret-long-enough")
    monkeypatch.setenv("RIGPILOT_ADMIN_PASSWORD", PASSWORD)
    from server import main as main_module
    from server.config import Settings

    catalog = main_module.ManagedMinerCatalog(fetcher=lambda *_: [])
    monkeypatch.setattr(main_module, "ManagedMinerCatalog", lambda: catalog)
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "owner-api.db",
        secret_key="task34-synthetic-controller-secret-long-enough",
        admin_username="admin", admin_password=PASSWORD,
    )
    make_app = lambda: main_module.create_app(settings)
    with ExitStack() as stack:
        api = SimpleNamespace(client=stack.enter_context(TestClient(make_app())))

        def reopen():
            stack.close()  # Release the real single-controller lock first.
            api.client = stack.enter_context(TestClient(make_app()))
            return api.client

        api.reopen = reopen
        yield api


def auth(token):
    return {"Authorization": "Bearer " + token}


def login(client, username="admin"):
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def new_user(client, admin_token, username, role="viewer"):
    response = client.post("/api/users", headers=auth(admin_token), json={
        "username": username, "password": PASSWORD, "role": role,
    })
    assert response.status_code == 201, response.text
    token = login(client, username)
    me = client.get("/api/auth/me", headers=auth(token))
    assert me.status_code == 200, me.text
    assert me.json()["username"] == username
    return token, user_id(client, username)


def user_id(client, username="admin"):
    with client.app.state.db.connection() as db:
        return db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]


def group(name="Alice", group_id=GROUP_A, agent_ids=None):
    return {"id": group_id, "name": name, "agent_ids": [AGENT_A] if agent_ids is None else agent_ids}


def get_groups(client, token, **params):
    response = client.get(OWNER_PATH, headers=auth(token), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def put_groups(client, token, expected_revision, groups, **params):
    return client.put(OWNER_PATH, headers=auth(token), params=params,
                      json={"expected_revision": expected_revision, "groups": groups})


def preference_row(client, user_id):
    with client.app.state.db.connection() as db:
        row = db.execute(
            "SELECT key,value,updated_by,updated_at,revision FROM controller_preferences WHERE key=?",
            (f"workload_owner_groups:{user_id}",),
        ).fetchone()
        return tuple(row) if row else None


def test_missing_record_is_uninitialized_read_only_and_requires_auth(owner_client):
    client = owner_client.client
    assert client.get(OWNER_PATH).status_code == 401
    assert client.put(OWNER_PATH, json={"expected_revision": 0, "groups": []}).status_code == 401
    token = login(client)
    account_id = user_id(client)
    assert get_groups(client, token) == {"initialized": False, "revision": 0, "groups": [], "updated_at": None}
    assert preference_row(client, account_id) is None


def test_saved_groups_survive_reopening_the_database(owner_client):
    client = owner_client.client
    token = login(client)
    saved_groups = [group("矿场 <&>", agent_ids=[AGENT_A, AGENT_B])]
    response = put_groups(client, token, 0, saved_groups)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["initialized"] is True and saved["revision"] == 1
    assert saved["groups"] == saved_groups and saved["updated_at"] is not None
    reopened = owner_client.reopen()
    assert get_groups(reopened, login(reopened)) == saved


def test_viewer_operator_and_admin_can_persist_their_own_groups(owner_client):
    client = owner_client.client
    admin_token = login(client)
    for role in ("viewer", "operator", "admin"):
        token, user_id = new_user(client, admin_token, "owner-" + role, role)
        assert get_groups(client, token)["initialized"] is False
        saved_groups = [group(role)]
        response = put_groups(client, token, 0, saved_groups)
        assert response.status_code == 200, response.text
        assert get_groups(client, token)["groups"] == saved_groups
        assert preference_row(client, user_id)[2] == user_id
    assert get_groups(client, admin_token)["initialized"] is False


def test_cross_account_queries_and_payloads_cannot_choose_another_owner(owner_client):
    client = owner_client.client
    admin_token = login(client)
    alice, alice_id = new_user(client, admin_token, "alice-owner")
    bob, bob_id = new_user(client, admin_token, "bob-owner")
    assert put_groups(client, alice, 0, [group("Alice")]).status_code == 200
    assert put_groups(client, bob, 0, [group("Bob", GROUP_B, [AGENT_B])]).status_code == 200
    bob_before = get_groups(client, bob)
    assert get_groups(client, alice, user_id=bob_id, username="bob-owner")["groups"] == [group("Alice")]
    response = put_groups(client, alice, 1, [group("Alice changed")], user_id=bob_id)
    assert response.status_code == 200, response.text
    forbidden = client.put(OWNER_PATH, headers=auth(alice), json={
        "expected_revision": 2, "groups": [], "user_id": bob_id,
    })
    assert forbidden.status_code == 422, forbidden.text
    assert get_groups(client, bob) == bob_before
    assert get_groups(client, alice)["groups"] == [group("Alice changed")]
    assert preference_row(client, alice_id)[0] != preference_row(client, bob_id)[0]


def test_username_rename_retains_the_immutable_user_id_record(owner_client):
    client = owner_client.client
    token, user_id = new_user(client, login(client), "owner-original")
    response = put_groups(client, token, 0, [group("Same account")])
    assert response.status_code == 200, response.text
    saved = response.json()
    with client.app.state.db.connection() as db:
        db.execute("UPDATE users SET username=? WHERE id=?", ("owner-renamed", user_id))
    assert get_groups(client, token) == saved
    assert get_groups(client, login(client, "owner-renamed")) == saved
    assert preference_row(client, user_id)[0] == f"workload_owner_groups:{user_id}"


def test_invalid_payloads_are_rejected_without_changing_the_record(owner_client):
    client = owner_client.client
    token = login(client)
    valid = {"expected_revision": 1, "groups": [group()]}
    assert put_groups(client, token, 0, valid["groups"]).status_code == 200
    before = get_groups(client, token)
    invalid = [
        {**valid, "expected_revision": value} for value in (True, -1, "1", 1.0, 2**63)
    ] + [
        {"groups": []}, {**valid, "groups": {}}, {**valid, "groups": ["not a group"]},
        {**valid, "unexpected": True},
        {"expected_revision": 1, "groups": [group(), group("Bob", GROUP_A, [AGENT_B])]},
        {"expected_revision": 1, "groups": [group(), group("Alice", GROUP_B, [AGENT_B])]},
        {"expected_revision": 1, "groups": [group(), group("Bob", GROUP_B, [AGENT_A])]},
        {"expected_revision": 1, "groups": [group(agent_ids=[AGENT_A, AGENT_A])]},
    ]
    for field, value in [("id", "bad id"), ("name", ""), ("name", "Bad\nName"), ("name", "\ud800"),
                         ("name", "A" * 81), ("agent_ids", ["bad id"]), ("agent_ids", [True]),
                         ("agent_ids", "not a list"), ("agent_ids", []), ("unexpected", True)]:
        bad = deepcopy(valid)
        bad["groups"][0][field] = value
        invalid.append(bad)
    invalid.append({"expected_revision": 1, "groups": [
        group(f"Owner-{index}", f"owner-{index}", [f"agent-{index}"]) for index in range(513)
    ]})
    invalid.append({"expected_revision": 1, "groups": [group(agent_ids=[f"agent-{index}" for index in range(4097)])]})
    oversized = {"expected_revision": 1, "groups": [
        group(f"Size-{index}", f"size-{index}", [f"{index}-{member}-".ljust(128, "x") for member in range(1024)])
        for index in range(8)
    ]}
    for index, body in enumerate(invalid):
        response = client.put(OWNER_PATH, headers={**auth(token), "Content-Type": "application/json"},
                              content=json.dumps(body, ensure_ascii=True))
        assert response.status_code == 422, (index, response.text[:500])
        assert get_groups(client, token) == before
    # The normal HTTP size guard rejects this envelope before model validation.
    response = client.put(OWNER_PATH, headers=auth(token), json=oversized)
    assert response.status_code == 413, response.text[:500]
    assert get_groups(client, token) == before


def test_empty_initialization_and_later_clear_do_not_resurrect_groups(owner_client):
    client = owner_client.client
    token = login(client)
    empty = put_groups(client, token, 0, [])
    assert empty.status_code == 200, empty.text
    assert empty.json()["initialized"] is True and empty.json()["revision"] == 1
    client = owner_client.reopen()
    token = login(client)
    assert get_groups(client, token) == empty.json()
    assert put_groups(client, token, 1, [group()]).status_code == 200
    cleared = put_groups(client, token, 2, [])
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["initialized"] is True and cleared.json()["groups"] == []
    assert put_groups(client, token, 1, [group()]).status_code == 409
    reopened = owner_client.reopen()
    assert get_groups(reopened, login(reopened)) == cleared.json()


def test_stale_revision_cannot_partially_write_any_preference_field(owner_client):
    client = owner_client.client
    token = login(client)
    account_id = user_id(client)
    assert put_groups(client, token, 0, [group()]).status_code == 200
    updated = put_groups(client, token, 1, [group("Changed", GROUP_B, [AGENT_B])])
    assert updated.status_code == 200, updated.text
    before = preference_row(client, account_id)
    stale = put_groups(client, token, 1, [group("Stale")])
    assert stale.status_code == 409, stale.text
    assert preference_row(client, account_id) == before
    assert get_groups(client, token) == updated.json()


def test_concurrent_create_and_update_each_have_one_compare_and_swap_winner(owner_client):
    client = owner_client.client
    token = login(client)
    expected = 0
    for stage in ("create", "update"):
        barrier = Barrier(2)

        def submit(index):
            barrier.wait(timeout=10)
            return put_groups(client, token, expected, [group(f"{stage}-{index}",
                GROUP_A if index == 0 else GROUP_B, [AGENT_A if index == 0 else AGENT_B])])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, index) for index in (0, 1)]
            responses = [future.result(timeout=20) for future in futures]
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(response.json() for response in responses if response.status_code == 200)
        assert winner["revision"] == expected + 1
        assert get_groups(client, token) == winner
        expected = winner["revision"]


def test_offline_and_unknown_form_valid_agent_ids_are_carried_without_dropping(owner_client):
    client = owner_client.client
    token = login(client)
    issued = client.post("/api/enrollment-tokens", headers=auth(token), json={"label": "task34-synthetic", "ttl_minutes": 30})
    assert issued.status_code == 200, issued.text
    enrolled = client.post("/api/agent/enroll", json={
        "enrollment_token": issued.json()["token"], "name": "offline-synthetic", "hostname": "offline-synthetic.local",
        "os_family": "windows", "agent_version": "0.8.26",
    })
    assert enrolled.status_code == 201, enrolled.text
    known_id = enrolled.json()["agent_id"]
    with client.app.state.db.connection() as db:
        db.execute("UPDATE agents SET last_seen=? WHERE id=?", ("2000-01-01T00:00:00+00:00", known_id))
        assert db.execute("SELECT id FROM agents WHERE id=?", (UNKNOWN_AGENT,)).fetchone() is None
    listed = client.get("/api/agents", headers=auth(token))
    assert listed.status_code == 200, listed.text
    assert next(agent for agent in listed.json() if agent["id"] == known_id)["status"] == "offline"
    owned = [group("Historical refs", agent_ids=[known_id, UNKNOWN_AGENT])]
    response = put_groups(client, token, 0, owned)
    assert response.status_code == 200, response.text
    assert get_groups(client, token)["groups"] == owned
    renamed = [group("Renamed refs", agent_ids=[known_id, UNKNOWN_AGENT])]
    assert put_groups(client, token, 1, renamed).status_code == 200
    assert get_groups(client, token)["groups"] == renamed
