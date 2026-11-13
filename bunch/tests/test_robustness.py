"""Small robustness fixes (#75): MCP argument types, config parsing, health check, start-up name clash, schema version."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from bunch import autoreply, config
from bunch.app import create_app
from bunch.config import Settings
from bunch.store import MIGRATIONS, Store

from .test_bunch import ADMIN, FakeLLM, adm, env, h, mcp  # noqa: F401  (env is a fixture)


def test_mcp_rejects_non_object_params(env):
    c, k, _, _ = env
    r = c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': [1, 2]}, headers=h(k['alice']))
    assert r.status_code == 200 and r.json()['error']['code'] == -32602


def test_mcp_rejects_non_object_arguments(env):
    c, k, _, _ = env
    out = mcp(c, k['alice'], 'tools/call', {'name': 'rooms', 'arguments': [1]})
    assert out['error']['code'] == -32602
    assert mcp(c, k['alice'], 'tools/call', {'arguments': {}})['error']['code'] == -32602


def test_config_bad_number_is_one_clear_line(monkeypatch):
    monkeypatch.setenv('BUNCH_MAX_CHAIN', 'twenty')
    with pytest.raises(SystemExit) as e:
        config._int('BUNCH_MAX_CHAIN', 6)
    assert 'BUNCH_MAX_CHAIN must be a number' in str(e.value)


def test_config_rejects_zero_and_negative(monkeypatch):
    monkeypatch.setenv('BUNCH_READ_LIMIT', '0')
    with pytest.raises(SystemExit):
        config._int('BUNCH_READ_LIMIT', 30)
    
    monkeypatch.setenv('BUNCH_MAX_CHAIN', '-1')
    with pytest.raises(SystemExit):
        config._int('BUNCH_MAX_CHAIN', 6)
    
    monkeypatch.setenv('BUNCH_MAX_CHAIN', '0')
    assert config._int('BUNCH_MAX_CHAIN', 6) == 0
    
    monkeypatch.delenv('BUNCH_MAX_CHAIN', raising=False)
    assert config._int('BUNCH_MAX_CHAIN', 6) == 6


def test_healthz_fails_when_the_database_does_not_answer(env):
    c, _, app, _ = env
    assert c.get('/healthz').status_code == 200
    app.state.hub.store.db.close()
    assert c.get('/healthz').status_code == 503


def test_human_name_cannot_be_an_existing_agent(tmp_path):
    path = str(tmp_path / 'hub.db')
    st = Store(path)
    st.x("INSERT INTO agents(name,key_hash,kind,created) VALUES('sam','x','agent',0)")
    st.db.close()
    with pytest.raises(SystemExit) as e:
        create_app(Settings(db_path=path, admin_token=ADMIN, human_name='sam'), FakeLLM())
    assert 'sam' in str(e.value)


def test_schema_version_is_recorded(tmp_path):
    path = str(tmp_path / 'v.db')
    Store(path).db.close()
    assert sqlite3.connect(path).execute('PRAGMA user_version').fetchone()[0] == len(MIGRATIONS)
    Store(path).db.close()


def test_forget_room_drops_idle_locks():
    import asyncio
    autoreply._room_locks.clear()
    autoreply._room_locks[(1, 'gone')] = asyncio.Lock()
    autoreply._room_locks[(1, 'kept')] = asyncio.Lock()
    # Lock the 'kept' lock to simulate it being held
    async def lock_kept():
        await autoreply._room_locks[(1, 'kept')].acquire()
        try:
            autoreply.forget_room('gone')
            assert list(autoreply._room_locks) == [(1, 'kept')]
        finally:
            autoreply._room_locks[(1, 'kept')].release()
    asyncio.run(lock_kept())
    autoreply._room_locks.clear()
