"""Search upgrades, cached loop detection and batched lists (#77)."""
import asyncio
import sqlite3

from starlette.requests import Request

from bunch import hub as hubmod, store as storemod
from bunch.app import LoginIn, PasswordIn, UserIn, create_app
from bunch.config import Settings
from bunch.hub import Hub
from bunch.store import Store


class NoLLM:
    enabled = False


def make_hub(tmp_path):
    st = Store(str(tmp_path / 'hub.db'))
    return Hub(st, Settings(max_chain=2), NoLLM())


def add(st, text, room='general', body=None, sender='worker', human=False, to=()):
    st.ensure_room(room)
    return st.add_message(room, sender, human, to, text, body, None, False)


def test_search_upgrade_and_triggers(tmp_path):
    path = str(tmp_path / 'old.db')
    db = sqlite3.connect(path)
    db.executescript(storemod.SCHEMA)
    db.execute('DROP INDEX messages_reply')
    db.execute("INSERT INTO messages(room,sender,summary,created) VALUES('general','worker','historic apricot',0)")
    db.execute('PRAGMA user_version=1')
    db.commit()
    db.close()
    st = Store(path)
    h = Hub(st, Settings(), NoLLM())
    assert 'messages_reply' in {r['name'] for r in st.q('PRAGMA index_list(messages)')}
    assert [m['id'] for m in h.search('apricot')] == [1]
    mid = add(st, 'short pear')
    assert h.search('pear')[0]['id'] == mid
    st.x('UPDATE messages SET summary=?,body=? WHERE id=?', ('new summary', 'buried plum', mid))
    assert not h.search('pear')
    assert h.search('plum')[0]['snippet'] == 'buried plum'
    st.delete_message(mid)
    assert not h.search('plum')
    st.db.close()
    # Reopening must not rebuild the index.
    original = storemod.MIGRATIONS[1]
    try:
        storemod.MIGRATIONS[1] = lambda db: (_ for _ in ()).throw(AssertionError('reran migration'))
        st = Store(path)
        assert Hub(st, Settings(), NoLLM()).search('apricot')
    finally:
        storemod.MIGRATIONS[1] = original
        st.db.close()


def test_search_literals_ranking_room_and_shape(tmp_path):
    h = make_hub(tmp_path)
    st = h.store
    first = add(st, 'apricot apricot')
    add(st, 'apricot ' + 'other ' * 100)
    add(st, 'apricot', room='other')
    add(st, 'foo OR a-b')
    result = h.search('apricot', '#GENERAL', limit=1)
    assert result[0]['id'] == first
    assert set(result[0]) == {'id', 'room', 'sender', 'human', 'to', 'summary', 'reply_to', 'created', 'snippet'}
    assert h.search('foo" OR')
    assert h.search('a-b')
    assert h.search('""') == []
    add(st, 'a long log', body='log ' * 100 + 'the needle is here ' + 'log ' * 100)
    snippet = h.search('needle')[0]['snippet']
    assert 'needle' in snippet and len(snippet) < 200
    assert snippet.startswith('…') and snippet.endswith('…')
    assert len(h.search('apric')) == 3  # last word is a prefix
    st.fts = False
    assert len(h.search('apric', 'general')) == 2
    assert h.search('a-b')


def test_search_without_fts5():
    class WithoutFTS:
        def execute(self, sql):
            raise sqlite3.OperationalError('no such module: fts5')
    storemod._search_index(WithoutFTS())


def test_stall_caches_shingles_and_not_stale_results(tmp_path, monkeypatch):
    h = make_hub(tmp_path)
    first = add(h.store, 'one two three four')
    mid = add(h.store, 'one two three four')
    calls = []
    original = hubmod._shingles
    def shingles(text):
        calls.append(text)
        return original(text)
    monkeypatch.setattr(hubmod, '_shingles', shingles)
    assert h.stall(h.store.message(mid)) == f'#{mid} repeats #{first}'
    assert h.stall(h.store.message(mid))
    assert len(calls) == 2
    h.store.x('UPDATE messages SET summary=? WHERE id=?', ('different words entirely', first))
    assert h.stall(h.store.message(mid)) is None
    h.store.delete_message(first)
    assert h.stall(h.store.message(mid)) is None
    for i in range(15):
        mid = add(h.store, f'unique content number {i}')
        h.stall(h.store.message(mid))
    assert len(h._shingle_cache['general']) <= hubmod.ECHO_LOOKBACK + 1


def test_room_overview_matches_existing_helpers(tmp_path):
    h = make_hub(tmp_path)
    st = h.store
    st.ensure_room('empty')
    first = add(st, 'goal', human=True)
    st.set_cursor('reader', 'general', first)
    add(st, 'answer', to=['reader'])
    add(st, 'another answer')
    add(st, '(worker offline; not answered)')
    add(st, 'private', room='private', to=['someone'])
    st.x("UPDATE rooms SET summary='rolling',summary_upto=? WHERE name='general'", (first,))
    for fallback in (False, True):
        for r in h.room_overview('reader', fallback):
            assert r['summary'] == h.room_summary(r['name'], fallback)
            if not fallback:
                assert r['paused'] == h.paused(r['name'])
    assert h.rooms('reader') == ('empty (not joined): (empty)\n'
                                'general (3 new, 1 for you): rolling (+3 newer)\n'
                                'private (not joined): recent: worker: private\n'
                                'agents: none  (*=active 10m)')


def endpoint(app, path):
    return next(r.endpoint for r in app.routes if r.path == path and (path != "/api/admin/users" or "POST" in r.methods))


def test_admin_list_shapes_and_batched_queries(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / 'hub.db'), admin_token='test-token'), NoLLM())
    h = app.state.hub
    st = h.store
    st.create_agent('worker')
    st.add_usage('worker', ask=3, ctx=4, sub=5, calls=6, cached=20)
    add(st, 'hello')
    st.ensure_room('empty')
    request = Request({'type': 'http', 'headers': [], 'session': {}})
    async def lists():
        return (await endpoint(app, '/api/admin/rooms')(request, 'Bearer test-token'),
                await endpoint(app, '/api/admin/agents')(request, 'Bearer test-token'))
    queries = []
    st.db.set_trace_callback(queries.append)
    rooms, agents = asyncio.run(lists())
    count = len(queries)
    assert rooms == [
        {'name': 'empty', 'summary': '', 'count': 0, 'last_id': 0, 'paused': False, 'bot': False, 'folder': None},
        {'name': 'general', 'summary': '', 'count': 1, 'last_id': 1, 'paused': False, 'bot': False, 'folder': None}]
    worker = next(a for a in agents if a['name'] == 'worker')
    assert worker == dict(name='worker', kind='agent', last_seen=None, description='', model=None,
                         daily_tokens=h.s.daily_tokens, budget_set=None, system=None,
                         ask_tokens_today=3, ctx_tokens_today=4, sub_tokens_today=5, sub_calls_today=6,
                         sub_cached_today=20, spent_today=10, state='manual', status_until=None,
                         worker={'mode': 'manual'}, runs_as=None, runs_on=None, machine=None,
                         first_responder=False, avatar=None)
    for i in range(4):
        st.create_agent(f'worker{i}')
        st.ensure_room(f'room{i}')
    queries.clear()
    asyncio.run(lists())
    assert len(queries) == count


def test_password_handlers_dispatch_to_thread(tmp_path, monkeypatch):
    app = create_app(Settings(db_path=str(tmp_path / 'hub.db'), admin_token='test-token'), NoLLM())
    st = app.state.hub.store
    dispatched = []
    async def to_thread(fn, *args):
        dispatched.append(fn.__name__)
        return fn(*args)
    monkeypatch.setattr(asyncio, 'to_thread', to_thread)
    request = Request({'type': 'http', 'headers': [], 'session': {}})
    async def scenario():
        await endpoint(app, '/api/admin/users')(request, UserIn(name='person', password='test-password'), 'Bearer test-token')
        await endpoint(app, '/api/auth/login')(LoginIn(name='person', password='test-password'), request)
        assert request.session['user'] == 'person'
        await endpoint(app, '/api/admin/users/{name}/password')(
            request, 'person', PasswordIn(password='new-password', current_password='test-password'), None)
    asyncio.run(scenario())
    assert dispatched == ['create_user', 'verify_user', 'verify_user', 'set_password']
    assert st.verify_user('person', 'new-password')
    assert not st.verify_user('person', 'test-password')


def test_machine_snapshots_preserve_agent_details(tmp_path):
    h = make_hub(tmp_path)
    st = h.store
    st.create_machine('workstation')
    st.create_agent('local', model='local/model')
    st.create_agent('worker')
    st.set_worker('worker', {'mode': 'worker', 'machine': 'workstation', 'power': 'answer'})
    h._hosts = {'local/model': 'workstation.example.ts.net'}
    machines = {m['name']: m for m in st.machines()}
    for a in st.agents():
        for method in (h.worker_state, h.runs_as, h.agent_machine):
            assert method(a, machines) == method(a)
        assert h.model_host(a['model'], machines) == h.model_host(a['model'])


def test_search_prefix_substring_fallback_and_quotes(tmp_path):
    h = make_hub(tmp_path)
    assert h.store.fts
    mid = add(h.store, 'input validation rules')
    assert [m['id'] for m in h.search('valid')] == [mid]
    assert [m['id'] for m in h.search('input valid')] == [mid]
    assert [m['id'] for m in h.search('lidation')] == [mid]  # mid-word: via the LIKE fallback
    assert h.search('say "hi')== []  # a stray quote must not raise
    add(h.store, 'say "hi" now')
    assert h.search('say "hi')
