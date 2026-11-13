"""Viewer bodies stay small until expanded, without using metered agent reads."""
import pytest

from bunch.hub import VIEWER_INLINE_BODY_CHARS
from .test_bunch import ADMIN, adm, env, h  # noqa: F401


@pytest.mark.parametrize('body', [None, '', 'hello', 'x' * VIEWER_INLINE_BODY_CHARS,
                                 '🍌' * (VIEWER_INLINE_BODY_CHARS + 1)])
def test_viewer_message_body_and_event(env, body):
    c, keys, app, _ = env
    hub = app.state.hub
    mid = hub.store.add_message('plans', 'alice', False, [], 'summary', body, None, False)
    with c.websocket_connect('/ws') as ws:
        ws.send_json({'token': ADMIN})
        assert ws.receive_json()['type'] == 'ready'
        c.portal.call(hub._notify, mid)
        event = ws.receive_json()
    rows = c.get('/api/admin/rooms/plans/messages', headers=adm()).json()
    catchup = c.get('/api/admin/rooms/plans/messages?after=0', headers=adm()).json()
    assert event == {'type': 'message', 'message': rows[0]}
    assert catchup == rows
    assert rows[0]['size'] == len(body or '')
    assert rows[0]['body'] == (body if len(body or '') <= VIEWER_INLINE_BODY_CHARS else None)
    usage = dict(hub.store.usage_today('alice'))
    full = c.get(f'/api/admin/messages/{mid}/body', headers=adm())
    assert full.status_code == 200
    assert full.json() == {'id': mid, 'body': body, 'size': len(body or '')}
    assert dict(hub.store.usage_today('alice')) == usage


def test_viewer_body_auth_missing_and_deleted(env):
    c, keys, app, _ = env
    st = app.state.hub.store
    mid = st.add_message('plans', 'alice', False, [], 'summary', 'long' * 1000, None, False)
    url = f'/api/admin/messages/{mid}/body'
    assert c.get(url).status_code == 401
    assert c.get(url, headers=h(keys['alice'])).status_code == 401
    st.create_user('pat', 'correct horse', admin=False)
    assert c.post('/api/auth/login', json={'name': 'pat', 'password': 'correct horse'}).status_code == 200
    assert c.get(url).json()['body'] == 'long' * 1000
    assert c.get('/api/admin/messages/999999/body').status_code == 404
    st.delete_message(mid)
    assert c.get(url).status_code == 404


@pytest.mark.parametrize('fts', [True, False])
def test_search_still_finds_long_body(env, fts):
    c, keys, app, _ = env
    st = app.state.hub.store
    if fts and not st.fts:
        pytest.skip('SQLite has no FTS5')
    st.fts = fts
    st.add_message('plans', 'alice', False, [], 'summary', 'x ' * 2000 + 'needle', None, False)
    rows = c.get('/api/admin/search?q=needle', headers=adm()).json()
    assert len(rows) == 1 and 'needle' in rows[0]['snippet']
    assert 'body' not in rows[0]
