"""Admin actions clean up after themselves and tell other viewers (#74)."""
import pytest

from bunch import cli

from .test_bunch import ADMIN, adm, env, h  # noqa: F401  (env is a fixture)


def _events(app, monkeypatch):
    """Collects what the hub broadcasts to viewers."""
    seen = []
    monkeypatch.setattr(app.state.hub, "broadcast", lambda ev: seen.append(ev["type"]))
    return seen


def test_deleting_an_agent_leaves_nothing_behind(env):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    c.put('/api/admin/agents/alice/first', json={'on': True}, headers=adm())
    st.add_usage('alice', ask=40)
    hub.held['alice'] = {1: None}
    assert c.delete('/api/admin/agents/alice', headers=adm()).status_code == 200
    assert not st.setting('first_responder')
    assert st.usage_today('alice')['ask'] == 0
    assert 'alice' not in hub.held


def test_agent_changes_reach_other_viewers(env, monkeypatch):
    c, k, app, llm = env
    hub = app.state.hub
    seen = _events(app, monkeypatch)
    ops = [
        lambda: c.post('/api/admin/agents', json={'name': 'dave'}, headers=adm()),
        lambda: c.put('/api/admin/agents/dave/info', json={'description': 'x'}, headers=adm()),
        lambda: c.post('/api/admin/agents/dave/rotate', headers=adm()),
        lambda: c.put('/api/admin/agents/dave/budget', json={'daily_tokens': 10}, headers=adm()),
        lambda: c.put('/api/admin/agents/dave/first', json={'on': True}, headers=adm()),
        lambda: c.delete('/api/admin/agents/dave', headers=adm()),
    ]
    for op in ops:
        assert op().status_code == 200
        assert seen == ['agents']
        seen.clear()


def test_budget_rejects_negatives_and_people(env):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    assert c.put('/api/admin/agents/alice/budget', json={'daily_tokens': -5}, headers=adm()).status_code == 400
    st.create_user('pat', 'correct horse', admin=False)
    assert c.put('/api/admin/agents/pat/budget', json={'daily_tokens': 5}, headers=adm()).status_code == 404
    assert c.put('/api/admin/agents/alice/budget', json={'daily_tokens': 0}, headers=adm()).status_code == 200


def test_agent_model_must_exist_on_the_gateway(env):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    assert c.put('/api/admin/agents/alice/info', json={'model': 'nope/missing'}, headers=adm()).status_code == 400
    assert c.put('/api/admin/agents/alice/info', json={'model': 'paid/big'}, headers=adm()).status_code == 200


def test_messages_route_normalises_the_room(env):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    c.post('/api/v1/post', json={'room': 'plans', 'text': 'hello'}, headers=h(k['alice']))
    a = c.get('/api/admin/rooms/plans/messages', headers=adm()).json()
    b = c.get('/api/admin/rooms/%23Plans/messages', headers=adm()).json()
    assert a == b and a


def test_cli_budget_and_remove_user(env, monkeypatch, capsys):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    monkeypatch.setenv('BUNCH_DB', hub.s.db_path)
    monkeypatch.setenv('BUNCH_ADMIN_TOKEN', ADMIN)
    assert cli.main(['budget', 'nobody', '5']) == 1 and 'no such agent' in capsys.readouterr().err
    assert cli.main(['budget', 'alice', '-1']) == 1
    assert cli.main(['budget', 'alice', '7']) == 0
    st.create_user('pat', 'correct horse', admin=False)
    assert cli.main(['remove-user', 'pat']) == 0
    assert cli.main(['remove-user', 'pat']) == 1
    assert cli.main(['add-machine', 'box']) == 0
    capsys.readouterr()
    assert cli.main(['machines']) == 0 and 'box' in capsys.readouterr().out


def test_cli_restore_brings_back_a_backup(env, monkeypatch, tmp_path, capsys):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    monkeypatch.setenv('BUNCH_DB', hub.s.db_path)
    monkeypatch.setenv('BUNCH_ADMIN_TOKEN', ADMIN)
    b = str(tmp_path / 'b.db')
    assert cli.main(['backup', b]) == 0
    c.delete('/api/admin/agents/bob', headers=adm())
    assert st.agent('bob') is None
    assert cli.main(['restore', b]) == 0
    assert st.agent('bob') is not None
    (tmp_path / 'junk').write_text('not a database')
    assert cli.main(['restore', str(tmp_path / 'junk')]) == 1


def test_deleting_a_message_removes_files_only_it_cited(env):
    c, k, app, llm = env
    hub = app.state.hub
    st = hub.store
    only = st.add_file("r", "a.png", "image/png", b"one", "alice")
    shared = st.add_file("r", "b.png", "image/png", b"two", "alice")
    first = int(hub.post("alice", "r", f"look ![a](file:{only}) and ![b](file:{shared})").split("#")[1].split()[0])
    hub.post("bob", "r", f"again ![b](file:{shared})")
    assert c.delete(f"/api/admin/messages/{first}", headers=adm()).status_code == 200
    assert st.file(only) is None and st.file(shared) is not None
