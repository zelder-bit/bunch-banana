"""Hub and API fixes: #131 #136 #137 #132 #128."""
from bunch.hub import VIEWER_INLINE_BODY_CHARS, Hub

from .test_bunch import adm, env, h, tool  # noqa: F401  (env is a fixture)


def test_message_json_full_keeps_long_body(env):  # #131
    _c, _keys, app, _ = env
    st = app.state.hub.store
    body = "x" * (VIEWER_INLINE_BODY_CHARS + 5)
    m = st.message(st.add_message("plans", "alice", False, [], "s", body, None, False))
    assert Hub.message_json(m)["body"] is None
    j = Hub.message_json(m, full=True)
    assert j["body"] == body and j["size"] == len(body)


def test_stale_first_responder_cleared_at_start(tmp_path, caplog):  # #136
    from fastapi.testclient import TestClient
    from bunch.app import create_app
    from bunch.config import Settings
    from bunch.store import Store
    from .test_bunch import ADMIN, FakeLLM
    db = str(tmp_path / "hub.db")
    st = Store(db)
    st.set_setting("first_responder", "ghost")
    st.db.close()
    s = Settings(db_path=db, admin_token=ADMIN, litellm_url="http://x")
    with caplog.at_level("WARNING", logger="bunch"), TestClient(create_app(s, FakeLLM())) as c:
        assert c.app.state.hub.store.setting("first_responder") == ""
    assert "ghost" in caplog.text


def test_401_is_logged_once_per_address_and_reason(env, caplog):  # #137
    c, keys, _app, _ = env
    with caplog.at_level("WARNING", logger="bunch"):
        assert c.get("/api/v1/read").status_code == 401
        assert c.get("/api/v1/read", headers=h("secret-wrong-key")).status_code == 401
        assert c.get("/api/v1/read", headers=h("secret-wrong-key")).status_code == 401  # same reason: not logged again
    lines = [r.getMessage() for r in caplog.records if "401 on" in r.getMessage()]
    assert len(lines) == 2
    assert "no key" in lines[0] and "unknown key" in lines[1]
    assert "secret-wrong-key" not in caplog.text
    assert c.get("/api/v1/read", headers=h(keys["alice"])).status_code == 200


def test_folder_write_needs_a_joined_room(env):  # #128
    c, keys, _app, _ = env
    room = "proj"
    assert c.post(f"/api/admin/rooms/{room}/messages", json={"room": room, "text": "hi"}, headers=adm()).status_code == 200
    assert c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "plain"}, headers=adm()).status_code == 200
    key = keys["alice"]
    put = lambda: c.put(f"/api/v1/folders/{room}/file", params={"path": "a.txt"}, content=b"x", headers=h(key))  # noqa: E731
    assert c.get(f"/api/v1/folders/{room}", headers=h(key)).status_code == 200  # reads stay open
    r = put()
    assert r.status_code == 400 and "read or post in #proj first, then write to its folder" in r.text
    out, err = tool(c, key, "files", room=room, op="write", path="a.txt", text="x")
    assert "read or post in #proj first" in out
    tool(c, key, "read", room=room)  # joins the room
    assert put().status_code == 200


def test_agent_whose_model_the_gateway_lacks_is_not_answered(env):  # #132
    import asyncio
    from bunch.autoreply import autoreply_once
    c, _keys, app, llm = env
    hub = app.state.hub

    async def hosts():
        llm.model_names = {"local/small"}
        return {}
    llm.model_hosts = hosts
    hub._hosts_at = 0  # drop the cache the startup pass filled
    c.post("/api/admin/agents", json={"name": "helper", "model": "local/gone"}, headers=adm())
    c.post("/api/admin/agents", json={"name": "fine", "model": "local/small"}, headers=adm())
    st = hub.store
    assert hub.worker_state(st.agent("helper")) == "auto"  # gateway not asked yet: unknown, as before
    asyncio.run(hub.refresh_model_hosts())
    assert hub._models == {"local/small"}
    assert hub.worker_state(st.agent("helper")) == "no model"
    assert hub.worker_state(st.agent("fine")) == "auto"
    calls = len(llm.calls)
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@helper there?"}, headers=adm())
    asyncio.run(autoreply_once(hub))
    last = c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]
    assert last["sender"] == "helper"
    assert "the gateway has no model local/gone, so helper can't answer; not answered" in last["summary"]
    assert len(llm.calls) == calls  # the model wasn't asked
    assert {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}["helper"]["state"] == "no model"
