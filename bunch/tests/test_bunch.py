import asyncio

import pytest
from fastapi.testclient import TestClient

from bunch.app import create_app
from bunch.config import Settings
from bunch.summarizer import summarize_once

ADMIN = "test-admin-token"


class FakeLLM:
    enabled = True

    def __init__(self):
        self.calls = []

    async def chat(self, model, messages, max_tokens, user=None):
        self.calls.append((model, messages, max_tokens, user))
        return f"answer from {model}", 100

    async def models(self):
        return ["local/small", "paid/big"]


@pytest.fixture
def env(tmp_path):
    s = Settings(db_path=str(tmp_path / "hub.db"), admin_token=ADMIN, litellm_url="http://x",
                 summary_model="local/small", summary_every=3, daily_tokens=250, max_chain=6,
                 posts_per_min=50, read_limit=5, join_tail=2, get_max=50)
    llm = FakeLLM()
    app = create_app(s, llm)
    with TestClient(app) as c:
        keys = {}
        for n in ("alice", "bob", "carol"):
            r = c.post("/api/admin/agents", json={"name": n}, headers=adm())
            assert r.status_code == 200, r.text
            keys[n] = r.json()["key"]
        yield c, keys, app, llm


def adm():
    return {"Authorization": f"Bearer {ADMIN}"}


def h(key):
    return {"Authorization": f"Bearer {key}"}


def mcp(c, key, method, params=None, mid=1):
    body = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}
    r = c.post("/mcp", json=body, headers=h(key))
    assert r.status_code == 200, r.text
    return r.json()


def tool(c, key, name, **args):
    res = mcp(c, key, "tools/call", {"name": name, "arguments": args})["result"]
    return res["content"][0]["text"], res["isError"]


def test_auth_required(env):
    c, keys, *_ = env
    assert c.post("/mcp", json={}).status_code == 401
    assert c.get("/api/v1/rooms", headers=h("nope")).status_code == 401
    assert c.get("/api/admin/agents", headers=h(keys["alice"])).status_code == 401


def test_mcp_handshake_and_tools(env):
    c, keys, *_ = env
    init = mcp(c, keys["alice"], "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "t", "version": "1"}})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=h(keys["alice"]))
    assert r.status_code == 202
    tools = mcp(c, keys["alice"], "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["read", "post", "get", "ask", "rooms"]
    # Tool definitions are sent to the model every turn; keep them small.
    import json
    assert len(json.dumps(tools)) < 2200


def test_addressing_and_cursors(env):
    c, k, *_ = env
    assert tool(c, k["alice"], "post", room="general", text="hello everyone")[0] == "ok #1"
    assert tool(c, k["alice"], "post", room="general", text="@bob please check the logs")[0] == "ok #2"
    assert tool(c, k["alice"], "post", room="general", text="for carol only", to="carol")[0] == "ok #3"

    # bob has never read general: gets join summary + tail of relevant messages, not carol's
    out, _ = tool(c, k["bob"], "read", room="general")
    assert out.startswith("joined general (3 msgs; latest below)")
    assert "#2 alice>you: @bob please check the logs" in out
    assert "carol only" not in out.split("\n", 1)[1]

    # second read: nothing new
    assert tool(c, k["bob"], "read", room="general")[0] == "no new messages"
    # bob replies with re=: auto-addressed to alice
    assert tool(c, k["bob"], "post", room="general", text="logs are clean", re=2)[0] == "ok #4"
    out, _ = tool(c, k["alice"], "read")
    assert out == "#4 [general] bob>you re#2: logs are clean"
    # all=true shows other-addressed messages
    tool(c, k["bob"], "post", room="general", text="note to carol", to="carol")
    assert "bob>carol" in tool(c, k["alice"], "read", room="general", all=True)[0]


def test_mentions_reach_unjoined_agents(env):
    c, k, *_ = env
    tool(c, k["alice"], "post", room="ops", text="routine chatter")
    tool(c, k["alice"], "post", room="ops", text="@carol need you here")
    out, _ = tool(c, k["carol"], "read")
    assert out == "#2 [ops] alice>you: @carol need you here"


def test_long_message_summary_and_paged_get(env):
    c, k, app, llm = env
    long = "Build log line\n" * 20
    tool(c, k["alice"], "post", room="build", text=long, summary="build passed, 20 lines")
    out, _ = tool(c, k["bob"], "read", room="build")
    assert "build passed, 20 lines (+299)" in out
    page, _ = tool(c, k["bob"], "get", id=1)
    assert "[chars 0-50 of 299]" in page and "(more: get id=1 offset=50)" in page
    page2, _ = tool(c, k["bob"], "get", id=1, offset=250)
    assert "more:" not in page2


def test_summarizer(env):
    c, k, app, llm = env
    tool(c, k["alice"], "post", room="r", text="x" * 400)  # no summary given -> auto truncated
    tool(c, k["alice"], "post", room="r", text="two")
    tool(c, k["alice"], "post", room="r", text="three")
    hub = app.state.hub
    n = asyncio.run(summarize_once(hub))
    assert n == 2  # one message summary + one room summary
    assert hub.store.message(1)["summary"] == "answer from local/small"
    assert hub.store.room("r")["summary"] == "answer from local/small"
    out, _ = tool(c, k["bob"], "read", room="r")
    assert out.startswith("joined r. summary: answer from local/small")


def test_chain_limit_and_human_reset(env):
    c, k, *_ = env
    for i in range(6):
        assert tool(c, k["alice" if i % 2 else "bob"], "post", room="loop", text=f"m{i}")[1] is False
    out, err = tool(c, k["alice"], "post", room="loop", text="again")
    assert err and "waiting for a human" in out
    paused = lambda: {r["name"]: r["paused"] for r in c.get("/api/admin/rooms", headers=adm()).json()}["loop"]  # noqa: E731
    assert paused() is True
    r = c.post("/api/admin/rooms/loop/messages", json={"room": "loop", "text": "carry on"}, headers=adm())
    assert r.status_code == 200
    assert paused() is False
    assert tool(c, k["alice"], "post", room="loop", text="thanks")[1] is False


def test_ask_budget_and_room_post(env):
    c, k, app, llm = env
    out, _ = tool(c, k["alice"], "ask", model="?")
    assert out == "models: local/small, paid/big"
    out, _ = tool(c, k["alice"], "ask", model="paid/big", prompt="hi")
    assert out == "answer from paid/big"
    assert llm.calls[-1][3] == "alice"  # spend attributed to the agent
    out, _ = tool(c, k["alice"], "ask", model="paid/big", prompt="hi", room="results")
    assert out.startswith("posted #1 [results] alice: answer from paid/big")
    tool(c, k["alice"], "ask", model="paid/big", prompt="hi")  # 300 >= 250
    out, err = tool(c, k["alice"], "ask", model="paid/big", prompt="hi")
    assert err and "budget" in out
    # budget is per agent
    assert tool(c, k["bob"], "ask", model="paid/big", prompt="hi")[1] is False


def test_rest_api_and_json_read(env):
    c, k, *_ = env
    r = c.post("/api/v1/post", json={"room": "dev", "text": "@bob ping"}, headers=h(k["alice"]))
    assert r.text == "ok #1"
    r = c.get("/api/v1/read", params={"format": "json"}, headers=h(k["bob"]))
    msgs = r.json()["messages"]
    assert msgs[0]["for_me"] and msgs[0]["sender"] == "alice" and "body" not in msgs[0]
    r = c.get("/api/v1/rooms", headers=h(k["bob"]))
    assert r.text.startswith("dev (0 new): recent: alice: @bob ping")
    r = c.get("/api/v1/read", params={"wait": 0.2}, headers=h(k["bob"]))
    assert r.text == "no new messages"


def test_bad_input(env):
    c, k, *_ = env
    assert tool(c, k["alice"], "post", room="Bad Room!", text="x")[1]
    assert tool(c, k["alice"], "get", id=999)[1]
    assert tool(c, k["alice"], "read", room="nope")[1]
    assert "unknown tool" in tool(c, k["alice"], "frobnicate")[0]
    assert mcp(c, k["alice"], "bogus/method")["error"]["code"] == -32601


def test_viewer_websocket_gets_live_messages(env):
    c, k, *_ = env
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert ws.receive_json()["type"] == "ready"
        c.post("/api/v1/post", json={"room": "live", "text": "hello viewer"}, headers=h(k["alice"]))
        ev = ws.receive_json()
        assert ev["type"] == "message" and ev["message"]["summary"] == "hello viewer"


def test_viewer_rejects_bad_token(env):
    c, *_ = env
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": "wrong"})
        with pytest.raises(Exception):
            ws.receive_json()


def test_admin_agent_lifecycle(env):
    c, k, *_ = env
    r = c.post("/api/admin/agents/alice/rotate", headers=adm())
    new = r.json()["key"]
    assert c.get("/api/v1/rooms", headers=h(k["alice"])).status_code == 401
    assert c.get("/api/v1/rooms", headers=h(new)).status_code == 200
    assert c.put("/api/admin/agents/bob/budget", json={"daily_tokens": 5}, headers=adm()).json()["daily_tokens"] == 5
    assert c.delete("/api/admin/agents/carol", headers=adm()).status_code == 200
    names = [a["name"] for a in c.get("/api/admin/agents", headers=adm()).json()]
    assert "carol" not in names and "human" in names


def test_posting_does_not_skip_unread(env):
    c, k, *_ = env
    tool(c, k["alice"], "post", room="g", text="start")
    tool(c, k["bob"], "read", room="g")
    tool(c, k["alice"], "post", room="g", text="@bob unread question")
    tool(c, k["bob"], "post", room="g", text="unrelated update")
    assert "unread question" in tool(c, k["bob"], "read", room="g")[0]


def test_underscore_names_do_not_match_as_wildcards(env):
    c, k, *_ = env
    for n in ("a_b", "axb"):
        k[n] = c.post("/api/admin/agents", json={"name": n}, headers=adm()).json()["key"]
    tool(c, k["alice"], "post", room="w", text="hi")
    tool(c, k["a_b"], "read", room="w")
    tool(c, k["alice"], "post", room="w", text="@axb only")
    assert tool(c, k["a_b"], "read", room="w")[0] == "no new messages"


def test_viewer_and_theme_assets_served(env):
    c, *_ = env
    assert "static/bunch.css" in c.get("/").text
    for path in ("/static/bunch.css", "/static/fonts/fredoka.woff2", "/static/fonts/nunito.woff2",
                 "/static/svg/orange.svg", "/static/svg/bunch.svg", "/static/svg/empty.svg"):
        assert c.get(path).status_code == 200, path


# --------------------------------------------------------------- human logins
def test_user_login_session_and_roles(env):
    c, k, app, _ = env
    st = app.state.hub.store
    st.create_user("pat", "correct horse", admin=False)
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/auth/login", json={"name": "pat", "password": "wrong pass"}).status_code == 401
    assert c.post("/api/auth/login", json={"name": "nobody", "password": "whatever1"}).status_code == 401
    r = c.post("/api/auth/login", json={"name": "Pat", "password": "correct horse"})
    assert r.status_code == 200 and r.json() == {"name": "pat", "admin": False}
    assert c.get("/api/auth/me").json() == {"name": "pat", "admin": False}
    # Members chat as themselves, but can't manage agents or people.
    assert c.post("/api/admin/rooms/general/messages", json={"room": "general", "text": "hi @alice."}).status_code == 200
    msg = c.get("/api/admin/rooms/general/messages").json()[-1]
    assert msg["sender"] == "pat" and msg["human"] and msg["to"] == ["alice"]
    assert c.post("/api/admin/agents", json={"name": "x"}).status_code == 403
    assert c.get("/api/admin/users").status_code == 403
    assert c.put("/api/admin/users/pat/password", json={"password": "new password"}).status_code == 200
    with c.websocket_connect("/ws") as ws:   # the login cookie is enough for live updates
        ws.send_json({})
        assert ws.receive_json()["type"] == "ready"
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401
    assert c.post("/api/auth/login", json={"name": "pat", "password": "new password"}).status_code == 200


def test_admin_manages_users_and_token_still_works(env):
    c, *_ = env
    assert c.get("/api/auth/me", headers=adm()).json() == {"name": "human", "admin": True}
    assert c.post("/api/admin/users", json={"name": "sam", "password": "short"}, headers=adm()).status_code == 400
    assert c.post("/api/admin/users", json={"name": "sam", "password": "long enough", "admin": True},
                  headers=adm()).status_code == 200
    assert c.post("/api/admin/users", json={"name": "alice", "password": "long enough"}, headers=adm()).status_code == 400
    assert [u["name"] for u in c.get("/api/admin/users", headers=adm()).json()] == ["sam"]
    assert c.post("/api/auth/login", json={"name": "sam", "password": "long enough"}).json()["admin"] is True
    assert c.post("/api/admin/agents", json={"name": "made-by-sam"}).status_code == 200
    assert c.delete("/api/admin/users/sam", headers=adm()).status_code == 200


def test_login_rate_limit(env):
    c, *_ = env
    for _ in range(10):
        assert c.post("/api/auth/login", json={"name": "x", "password": "y"}).status_code == 401
    assert c.post("/api/auth/login", json={"name": "x", "password": "y"}).status_code == 429


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from bunch.store import Store
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE agents(name TEXT PRIMARY KEY, key_hash TEXT UNIQUE, kind TEXT NOT NULL DEFAULT 'agent', "
               "daily_tokens INTEGER, created REAL NOT NULL, last_seen REAL)")
    db.execute("INSERT INTO agents(name,kind,created) VALUES('human','human',0)")
    db.commit(); db.close()
    st = Store(path)
    st.create_user("pat", "password1")
    assert st.verify_user("pat", "password1")["name"] == "pat"
    assert st.agent("human")["description"] == ""


# --------------------------------------------------- routing and auto agents
def test_router_addresses_unmentioned_human_messages(env):
    c, k, app, llm = env
    hub = app.state.hub
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})
    hub.store.set_agent_info("bob", description="knows databases")

    async def pick_bob(model, messages, max_tokens, user=None):
        assert user == "_router" and "knows databases" in messages[1]["content"]
        return "bob", 10
    llm.chat = pick_bob
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "why is the db slow?"}, headers=adm())
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["to"] == ["bob"]
    assert "why is the db slow" in tool(c, k["bob"], "read")[0]

    async def broken(*a, **kw):
        raise RuntimeError("gateway down")
    llm.chat = broken
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "anyone?"}, headers=adm())
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["to"] == []
    # Already addressed: the router isn't asked.
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@carol you"}, headers=adm())
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["to"] == ["carol"]


def test_auto_agent_answers(env):
    from bunch.autoreply import autoreply_once
    c, k, app, llm = env
    hub = app.state.hub
    r = c.post("/api/admin/agents", json={"name": "helper", "model": "local/small", "description": "general help"},
               headers=adm())
    assert r.status_code == 200
    agents = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}
    assert agents["helper"]["model"] == "local/small" and agents["helper"]["description"] == "general help"
    hub.post("human", "lobby", "@helper what's up?", human=True)
    hub.post("alice", "lobby", "re: helper, no explicit mention", to="helper")  # agent without @: loop guard
    asyncio.run(autoreply_once(hub))
    msgs = c.get("/api/admin/rooms/lobby/messages", headers=adm()).json()
    answers = [m for m in msgs if m["sender"] == "helper"]
    assert len(answers) == 1 and answers[0]["summary"] == "answer from local/small" and answers[0]["to"] == ["human"]
    asyncio.run(autoreply_once(hub))  # nothing answered twice
    assert len([m for m in c.get("/api/admin/rooms/lobby/messages", headers=adm()).json() if m["sender"] == "helper"]) == 1


def test_mcp_read_wait_returns_when_empty(env):
    c, k, *_ = env
    text, err = tool(c, k["alice"], "read", wait=1)
    assert not err and text == "no new messages"


# ---------------------------------------------------------- viewer catch-up
def test_viewer_messages_after_id(env):
    c, k, *_ = env
    for i in range(4):
        tool(c, k["alice"], "post", room="cu", text=f"m{i}")
    r = c.get("/api/admin/rooms/cu/messages", params={"after": 2}, headers=adm())
    assert [m["summary"] for m in r.json()] == ["m2", "m3"]
    r = c.get("/api/admin/rooms/cu/messages", params={"after": 0, "limit": 2}, headers=adm())
    assert [m["id"] for m in r.json()] == [1, 2]


def test_password_change_signs_out_other_sessions(env):
    c, k, app, _ = env
    app.state.hub.store.create_user("pat", "first password")
    other = TestClient(app)
    for cl in (c, other):
        assert cl.post("/api/auth/login", json={"name": "pat", "password": "first password"}).status_code == 200
    # pat changes their own password: this session stays in, the other one is signed out.
    assert c.put("/api/admin/users/pat/password", json={"password": "second password"}).status_code == 200
    assert c.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401
    # an admin reset signs pat out everywhere
    assert c.put("/api/admin/users/pat/password", json={"password": "third password"}, headers=adm()).status_code == 200
    c.headers.pop("Authorization", None)
    assert c.get("/api/auth/me").status_code == 401


def test_failed_logins_are_pruned(env, monkeypatch):
    import bunch.app as appmod
    c, *_ = env
    now = [1000.0]
    monkeypatch.setattr(appmod.time, "time", lambda: now[0])
    for _ in range(10):
        c.post("/api/auth/login", json={"name": "x", "password": "y"})
    assert c.post("/api/auth/login", json={"name": "x", "password": "y"}).status_code == 429
    now[0] += 301  # old failures expire, and the entry is dropped rather than kept forever
    assert c.post("/api/auth/login", json={"name": "x", "password": "y"}).status_code == 401


def test_secure_cookie_setting(tmp_path):
    s = Settings(db_path=str(tmp_path / "s.db"), admin_token=ADMIN, secure_cookies=True)
    app = create_app(s, FakeLLM())
    app.state.hub.store.create_user("pat", "first password")
    with TestClient(app, base_url="https://testserver") as c:
        r = c.post("/api/auth/login", json={"name": "pat", "password": "first password"})
        assert "secure" in r.headers["set-cookie"].lower()


def test_admin_flag_and_last_admin_guard(env):
    c, k, app, _ = env
    st = app.state.hub.store
    st.create_user("ann", "password one", admin=True)
    st.create_user("ben", "password two")
    assert c.put("/api/admin/users/ben/admin", json={"admin": True}, headers=adm()).json()["admin"] is True
    assert c.put("/api/admin/users/ann/admin", json={"admin": False}, headers=adm()).status_code == 200
    assert c.put("/api/admin/users/alice/admin", json={"admin": True}, headers=adm()).status_code == 404
    # ben is now the only admin: signed in, he can't demote or delete himself
    c.post("/api/auth/login", json={"name": "ben", "password": "password two"})
    r = c.put("/api/admin/users/ben/admin", json={"admin": False})
    assert r.status_code == 400 and "last admin" in r.text
    assert c.delete("/api/admin/users/ben").status_code == 400
    assert c.delete("/api/admin/users/ann").status_code == 200
    # ...but the admin token is never locked out
    assert c.put("/api/admin/users/ben/admin", json={"admin": False}, headers=adm()).status_code == 200
    # members can't flip the flag
    st.create_user("cat", "password three")
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"name": "cat", "password": "password three"})
    assert c.put("/api/admin/users/cat/admin", json={"admin": True}).status_code == 403


def test_agent_list_shows_persona_to_admins_only(env):
    c, k, app, _ = env
    st = app.state.hub.store
    c.put("/api/admin/agents/alice/info", json={"model": "local/small", "system": "be terse"}, headers=adm())
    c.put("/api/admin/agents/bob/budget", json={"daily_tokens": 0}, headers=adm())
    rows = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}
    assert rows["alice"]["system"] == "be terse" and rows["alice"]["budget_set"] is None
    assert rows["bob"]["budget_set"] == 0
    st.create_user("pat", "password one")
    c.post("/api/auth/login", json={"name": "pat", "password": "password one"})
    rows = {a["name"]: a for a in c.get("/api/admin/agents").json()}
    assert rows["alice"]["system"] is None


# ------------------------------------------------------------------- search
def test_viewer_search(env):
    c, k, *_ = env
    tool(c, k["alice"], "post", room="dev", text="the Deploy failed on staging")
    tool(c, k["bob"], "post", room="ops", text="deploy fixed")
    long = "x" * 400 + " the needle is here " + "y" * 400
    tool(c, k["bob"], "post", room="ops", text=long, summary="a long log")
    tool(c, k["bob"], "post", room="ops", text="100% done_ok")
    r = c.get("/api/admin/search", params={"q": "deploy"}, headers=adm()).json()
    assert [m["room"] for m in r] == ["ops", "dev"]  # newest first, case-insensitive
    assert c.get("/api/admin/search", params={"q": "deploy", "room": "#dev"}, headers=adm()).json()[0]["id"] == 1
    hit = c.get("/api/admin/search", params={"q": "needle"}, headers=adm()).json()[0]
    assert "body" not in hit and "needle" in hit["snippet"] and len(hit["snippet"]) < 200
    assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…")
    assert len(c.get("/api/admin/search", params={"q": "0%"}, headers=adm()).json()) == 1   # % is literal
    assert c.get("/api/admin/search", params={"q": "o_e"}, headers=adm()).json() == []      # _ is literal
    assert c.get("/api/admin/search", params={"q": "x"}, headers=adm()).status_code == 400
    assert c.get("/api/admin/search", params={"q": "deploy"}).status_code == 401


def test_auto_agent_sends_typing_events(env):
    from bunch.autoreply import autoreply_once
    c, k, app, llm = env
    hub = app.state.hub
    c.post("/api/admin/agents", json={"name": "helper", "model": "local/small"}, headers=adm())
    q = asyncio.Queue()
    hub.listeners.add(q)
    hub.post("human", "lobby", "@helper hi", human=True)
    asyncio.run(autoreply_once(hub))
    events = [q.get_nowait() for _ in range(q.qsize())]
    kinds = [(e["type"], e.get("agent"), e.get("done", False)) for e in events]
    assert kinds == [("message", None, False), ("typing", "helper", False), ("typing", "helper", True),
                     ("message", None, False)]
    assert events[1]["room"] == "lobby"


# --------------------------------------------------------------- moderation
def test_admin_deletes_message(env):
    c, k, app, _ = env
    tool(c, k["alice"], "post", room="mod", text="oops my key is sk-123")
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert ws.receive_json()["type"] == "ready"
        assert c.delete("/api/admin/messages/1", headers=adm()).json() == {"deleted": 1}
        assert ws.receive_json() == {"type": "deleted", "id": 1, "room": "mod"}
    assert c.get("/api/admin/rooms/mod/messages", headers=adm()).json() == []
    assert tool(c, k["bob"], "get", id=1)[1]  # gone for agents too
    assert c.delete("/api/admin/messages/1", headers=adm()).status_code == 404
    app.state.hub.store.create_user("pat", "password one")
    c.post("/api/auth/login", json={"name": "pat", "password": "password one"})
    tool(c, k["alice"], "post", room="mod", text="another")
    assert c.delete("/api/admin/messages/2").status_code == 403


def test_deleting_a_summarized_message_resets_the_room_summary(env):
    c, k, app, _ = env
    st = app.state.hub.store
    for i in range(3):
        tool(c, k["alice"], "post", room="s", text=f"m{i}")
    st.x("UPDATE rooms SET summary='mentions m1', summary_upto=3 WHERE name='s'")
    c.delete("/api/admin/messages/2", headers=adm())
    assert st.room("s")["summary"] == "" and st.room("s")["summary_upto"] == 0


# ------------------------------------------------------------------- backup
def test_cli_backup(env, tmp_path, monkeypatch, capsys):
    import sqlite3
    from bunch import cli
    c, k, app, _ = env
    tool(c, k["alice"], "post", room="b", text="keep me")
    monkeypatch.setenv("BUNCH_DB", app.state.hub.s.db_path)
    out = str(tmp_path / "copy.db")
    assert cli.main(["backup", out]) == 0
    db = sqlite3.connect(out)
    assert db.execute("SELECT summary FROM messages").fetchone()[0] == "keep me"
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert cli.main(["backup", app.state.hub.s.db_path]) == 1   # refuses to overwrite the live file
    assert "live database" in capsys.readouterr().err


def test_admin_deletes_room(env):
    c, k, app, _ = env
    st = app.state.hub.store
    tool(c, k["alice"], "post", room="scratch", text="one")
    tool(c, k["bob"], "post", room="scratch", text="@alice two")
    tool(c, k["alice"], "post", room="keep", text="stays")
    tool(c, k["bob"], "read", room="scratch")
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert ws.receive_json()["type"] == "ready"
        assert c.delete("/api/admin/rooms/%23Scratch", headers=adm()).json() == {"deleted": "scratch", "messages": 2}
        assert ws.receive_json() == {"type": "room_deleted", "room": "scratch"}
    assert [r["name"] for r in c.get("/api/admin/rooms", headers=adm()).json()] == ["keep"]
    assert st.one("SELECT COUNT(*) AS n FROM cursors WHERE room='scratch'")["n"] == 0
    assert tool(c, k["alice"], "read")[0] == "no new messages"   # nothing left over for agents
    assert tool(c, k["bob"], "read", room="scratch")[1]           # the room is gone
    assert c.delete("/api/admin/rooms/scratch", headers=adm()).status_code == 404
    tool(c, k["alice"], "post", room="scratch", text="fresh start")  # posting makes it again, empty
    assert len(c.get("/api/admin/rooms/scratch/messages", headers=adm()).json()) == 1
    st.create_user("pat", "password one")
    c.post("/api/auth/login", json={"name": "pat", "password": "password one"})
    assert c.delete("/api/admin/rooms/keep").status_code == 403


# --------------------------------------------------- machines and their CLI agents
def _load_worker():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "clients" / "worker.py"
    spec = importlib.util.spec_from_file_location("bunch_worker", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CLIS = [{"cli": "claude", "version": "2.1", "models": ["haiku", "sonnet"]}, {"cli": "codex", "version": "0.1", "models": []}]


def _machine(c, name="workstation", clis=CLIS, addresses=("workstation", "192.0.2.7")):
    r = c.post("/api/admin/machines", json={"name": name}, headers=adm())
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    r = c.post("/api/v1/machine/hello", json={"hostname": name, "addresses": list(addresses), "clis": clis,
                                              "max_power": "read", "dirs": ["/srv/app"]}, headers=h(key))
    assert r.status_code == 200, r.text
    return key


def _on(c, agent, machine="workstation", **cfg):
    r = c.put(f"/api/admin/agents/{agent}/worker",
              json={"mode": "worker", "machine": machine, "cli": "claude", **cfg}, headers=adm())
    assert r.status_code == 200, r.text
    return r.json()


def test_old_usage_table_is_migrated(tmp_path):
    import sqlite3
    from bunch.store import Store
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE usage(agent TEXT NOT NULL, day TEXT NOT NULL, ask_tokens INTEGER NOT NULL DEFAULT 0, "
               "ctx_tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(agent, day))")
    db.commit(); db.close()
    st = Store(path)
    st.add_usage("x", ask=5, sub=40, calls=2)
    assert st.usage_today("x") == {"ask": 5, "ctx": 0, "sub": 40, "calls": 2, "cached": 0}
    assert st.machines() == []


def test_machine_pairing_and_hello(env):
    c, k, app, _ = env
    assert c.post("/api/admin/machines", json={"name": "pc"}, headers=h(k["alice"])).status_code == 401
    key = _machine(c)
    assert c.post("/api/admin/machines", json={"name": "workstation"}, headers=adm()).status_code == 400
    # A machine key is not an agent key, and the other way round.
    assert c.get("/api/v1/rooms", headers=h(key)).status_code == 401
    assert c.get("/api/v1/machine/read", headers=h(k["alice"])).status_code == 401
    m = c.get("/api/admin/machines", headers=adm()).json()[0]
    assert m["name"] == "workstation" and m["online"] and [x["cli"] for x in m["clis"]] == ["claude", "codex"]
    assert m["max_power"] == "read" and m["dirs"] == ["/srv/app"]
    # Agents can only go on a machine that exists, with a CLI it has.
    assert c.put("/api/admin/agents/alice/worker", json={"mode": "worker", "machine": "nope", "cli": "claude"},
                 headers=adm()).status_code == 400
    r = c.put("/api/admin/agents/alice/worker", json={"mode": "worker", "machine": "workstation", "cli": "agy"},
              headers=adm())
    assert r.status_code == 400 and "doesn't have agy" in r.text
    assert c.put("/api/admin/agents/alice/worker", json={"mode": "worker", "machine": "workstation", "cli": "claude",
                                                         "power": "read"}, headers=adm()).status_code == 400
    _on(c, "alice", model="sonnet")
    got = c.post("/api/v1/machine/hello", json={"clis": CLIS}, headers=h(key)).json()
    assert got["machine"] == "workstation" and [a["name"] for a in got["agents"]] == ["alice"]
    agents = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}
    assert agents["alice"]["state"] == "online" and agents["alice"]["worker"]["machine"] == "workstation"
    assert agents["bob"]["state"] == "manual"
    # Creating an agent straight onto a machine hands out no key: the machine speaks for it.
    r = c.post("/api/admin/agents", json={"name": "coder", "worker": {"mode": "worker", "machine": "workstation",
                                                                      "cli": "codex"}}, headers=adm())
    assert r.status_code == 200 and "key" not in r.json() and r.json()["machine"] == "workstation"
    # Deleting the machine leaves its agents, unassigned.
    r = c.delete("/api/admin/machines/workstation", headers=adm())
    assert sorted(r.json()["agents_unassigned"]) == ["alice", "coder"]
    assert c.post("/api/v1/machine/hello", json={}, headers=h(key)).status_code == 401


def test_machine_reads_jobs_and_speaks_only_for_its_agents(env):
    c, k, app, _ = env
    key = _machine(c)
    other = _machine(c, "laptop")
    _on(c, "alice")
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@alice hi from the laptop"}, headers=adm())
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "room chatter"}, headers=adm())
    jobs = c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"]
    assert [(j["agent"], j["text"]) for j in jobs] == [("alice", "@alice hi from the laptop")]
    assert jobs[0]["human"] and jobs[0]["parent"] is None
    assert c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"] == []
    r = c.post("/api/v1/machine/post", json={"agent": "alice", "room": "r", "text": "hello!", "re": jobs[0]["id"]},
               headers=h(key))
    assert r.status_code == 200
    assert c.post("/api/v1/machine/post", json={"agent": "alice", "room": "r", "text": "x"},
                  headers=h(other)).status_code == 403
    assert c.post("/api/v1/machine/post", json={"agent": "bob", "room": "r", "text": "x"},
                  headers=h(key)).status_code == 403
    assert c.post("/api/v1/machine/report", json={"agent": "alice", "status": "online", "tokens": 30, "calls": 1},
                  headers=h(key)).text == "ok 30/250"


def test_offline_machine_says_so_and_drops_the_message(env):
    import time
    c, k, app, _ = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    hub.store.x("UPDATE machines SET last_seen=? WHERE name='workstation'", (time.time() - 600,))
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@alice are you there?"}, headers=adm())
    msgs = c.get("/api/admin/rooms/r/messages", headers=adm()).json()
    assert msgs[-1]["sender"] == "alice" and "workstation is offline" in msgs[-1]["summary"]
    assert msgs[-1]["reply_to"] == msgs[-2]["id"] and msgs[-1]["to"] == ["human"]
    # The machine comes back: the dropped message isn't answered late.
    assert c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"] == []
    # Out of quota is the same: an immediate note instead of silence.
    hub.store.set_status("alice", "cooling", time.time() + 600)
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@alice again?"}, headers=adm())
    assert "out of claude quota on workstation" in c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["summary"]
    assert c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"] == []


def test_worker_report_tracks_usage_budget_and_cooling(env):
    import time
    c, k, app, _ = env
    key = _machine(c)
    _on(c, "alice", cli="codex")
    rep = lambda **b: c.post("/api/v1/machine/report", json={"agent": "alice", **b}, headers=h(key))
    assert rep(status="online", tokens=100, calls=1).text == "ok 100/250"
    assert rep(status="sleepy").status_code == 400
    # The daily budget covers gateway asks and CLI runs together.
    c.put("/api/admin/agents/alice/worker", json={"mode": "manual"}, headers=adm())
    tool(c, k["alice"], "ask", model="local/small", prompt="hi")  # FakeLLM: 100 tokens
    _on(c, "alice", cli="codex")
    assert rep(status="online", tokens=60, calls=1).text == "over budget 260/250"
    rep(status="cooling", until=time.time() + 600)
    a = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}["alice"]
    assert a["state"] == "cooling" and a["sub_tokens_today"] == 160 and a["sub_calls_today"] == 2
    from bunch.app import _worker_view
    cfg = {"mode": "worker", "machine": "m", "cli": "claude", "power": "read", "model": "", "dir": "/srv/x"}
    assert "dir" not in _worker_view(cfg, False) and _worker_view(cfg, True) == cfg


def test_router_prefers_cheap_and_skips_cooling_and_offline(env):
    import time
    c, k, app, llm = env
    hub = app.state.hub
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})
    _machine(c)
    _machine(c, "laptop")
    _on(c, "alice")
    _on(c, "bob")
    _on(c, "carol", machine="laptop")
    hub.store.set_status("bob", "cooling", time.time() + 600)
    hub.store.x("UPDATE machines SET last_seen=? WHERE name='laptop'", (time.time() - 600,))
    seen = {}

    async def pick(model, messages, max_tokens, user=None):
        seen["roster"] = messages[1]["content"]
        return "bob, carol, alice", 10
    llm.chat = pick
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "hard question"}, headers=adm())
    roster = seen["roster"].split("Room #")[0]
    assert "alice: no description (subscription claude on workstation)" in roster
    assert "bob" not in roster and "carol" not in roster
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["to"] == ["alice"]


def test_router_first_responder_and_one_auto_agent(env):
    c, k, app, llm = env
    hub = app.state.hub
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})
    for n in ("llama", "qwen"):
        c.post("/api/admin/agents", json={"name": n, "model": "local/small"}, headers=adm())

    async def both(model, messages, max_tokens, user=None):
        return "llama, qwen", 10
    llm.chat = both
    last = lambda room="r": [m for m in c.get(f"/api/admin/rooms/{room}/messages", headers=adm()).json()
                             if m["human"]][-1]["to"]  # the auto agents may already have replied
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "what is the capital of peru"}, headers=adm())
    assert last() == ["llama"]  # never two local models on one question
    assert c.put("/api/admin/agents/qwen/first", json={"on": True}, headers=adm()).status_code == 200
    agents = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}
    assert agents["qwen"]["first_responder"] and not agents["llama"]["first_responder"]

    async def never(*a, **kw):
        raise AssertionError("router asked despite a first responder")
    llm.chat = never
    c.post("/api/admin/rooms/r2/messages", json={"room": "r2", "text": "and the capital of chile"}, headers=adm())
    assert last("r2") == ["qwen"]
    c.put("/api/admin/agents/qwen/first", json={"on": False}, headers=adm())
    assert not any(a.get("first_responder") for a in c.get("/api/admin/agents", headers=adm()).json())


def _hosts(app, llm, table):
    async def hosts():
        return table
    llm.model_hosts = hosts
    app.state.hub._hosts_at = 0  # drop the cache the startup pass filled


def test_gateway_models_show_which_machine_they_run_on(env):
    c, k, app, llm = env
    _hosts(app, llm, {"local/small": "192.0.2.7", "paid/big": "api.example.com"})
    _machine(c)
    c.post("/api/admin/agents", json={"name": "helper", "model": "local/small"}, headers=adm())
    c.post("/api/admin/agents", json={"name": "big", "model": "paid/big"}, headers=adm())
    agents = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}
    assert agents["helper"]["runs_on"] == "workstation" and agents["big"]["runs_on"] == "api.example.com"
    assert agents["alice"]["runs_on"] is None
    # Grouped by the computer that answers: the local model lives on workstation, the API on no machine.
    assert agents["helper"]["machine"] == "workstation" and agents["big"]["machine"] is None
    models = {m["name"]: m for m in c.get("/api/admin/models", headers=adm()).json()}
    assert {k: models["local/small"][k] for k in ("name", "runs_on", "machine")} == \
        {"name": "local/small", "runs_on": "workstation", "machine": "workstation"}
    assert models["local/small"]["used_by"] == ["helper", "_summarizer"]
    assert models["paid/big"]["machine"] is None and models["paid/big"]["runs_on"] == "api.example.com"


def test_gateway_agent_goes_offline_with_its_machine(env):
    import time
    from bunch.autoreply import autoreply_once
    c, k, app, llm = env
    hub = app.state.hub
    _hosts(app, llm, {"local/small": "workstation.example.ts.net"})
    _machine(c)
    c.post("/api/admin/agents", json={"name": "helper", "model": "local/small"}, headers=adm())
    asyncio.run(hub.refresh_model_hosts())
    hub.store.x("UPDATE machines SET last_seen=? WHERE name='workstation'", (time.time() - 600,))
    calls = len(llm.calls)
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@helper still there?"}, headers=adm())
    asyncio.run(autoreply_once(hub))
    msgs = c.get("/api/admin/rooms/r/messages", headers=adm()).json()
    assert msgs[-1]["sender"] == "helper" and "workstation is offline, so local/small can't answer" in msgs[-1]["summary"]
    assert len(llm.calls) == calls  # the model wasn't asked
    assert {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}["helper"]["state"] == "offline"


def test_gateway_error_gets_a_note_not_silence(env):
    from bunch.autoreply import autoreply_once
    c, k, app, llm = env
    hub = app.state.hub
    c.post("/api/admin/agents", json={"name": "helper", "model": "local/small"}, headers=adm())

    async def down(*a, **kw):
        from bunch.llm import LLMError
        raise LLMError("gateway unreachable: ConnectError")
    llm.chat = down
    hub.post("human", "r", "@helper hi", human=True)
    asyncio.run(autoreply_once(hub))
    last = c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]
    assert last["sender"] == "helper" and "local/small didn't answer: gateway unreachable" in last["summary"]
    asyncio.run(autoreply_once(hub))  # not retried or repeated
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["id"] == last["id"]


def test_worker_power_is_clamped_by_host():
    w = _load_worker()
    cfg = {"cli": "claude", "power": "full", "dir": "/srv/app/sub"}
    assert w.effective_power(cfg, "answer", ["/srv/app"]) == ("answer", "")
    assert w.effective_power(cfg, "read", ["/srv/app"]) == ("read", "/srv/app/sub")
    assert w.effective_power(cfg, "full", ["/srv/app"]) == ("full", "/srv/app/sub")
    assert w.effective_power(cfg, "full", ["/srv/other"]) == ("answer", "")
    assert w.effective_power({**cfg, "dir": "/srv/application"}, "full", ["/srv/app"]) == ("answer", "")


def test_worker_builds_lean_cli_commands():
    w = _load_worker()
    argv, cwd, stdin = w.build_cmd({"cli": "claude", "model": "sonnet", "deep_model": "opus"}, "be brief",
                                   "answer", "/tmp/x", "the prompt")
    assert argv[:2] == ["claude", "-p"] and cwd == "/tmp/x" and stdin == "the prompt"
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--system-prompt") + 1] == "be brief"
    assert argv[argv.index("--model") + 1] == "sonnet"
    argv, _, _ = w.build_cmd({"cli": "claude", "model": "sonnet", "deep_model": "opus"}, "s", "full", "/d", "p", True)
    assert argv[argv.index("--model") + 1] == "opus" and "acceptEdits" in argv and "--system-prompt" not in argv
    argv, _, stdin = w.build_cmd({"cli": "codex", "model": ""}, "sys", "read", "/d", "p")
    assert argv[argv.index("-s") + 1] == "read-only" and "-m" not in argv and argv[-1] == "-" and stdin == "sys\n\np"
    argv, _, _ = w.build_cmd({"cli": "codex"}, "s", "full", "/d", "p")
    assert argv[argv.index("-s") + 1] == "workspace-write"
    # agy ignores stdin, so the prompt goes in -p (last, so no flag can swallow it).
    argv, _, stdin = w.build_cmd({"cli": "agy", "model": "gemini-3.8-flash-low"}, "sys", "answer", "/d", "p")
    assert argv[-2:] == ["-p", "sys\n\np"] and stdin is None and "--sandbox" in argv
    assert "--mode" not in argv and "--disable-slash-commands" in argv  # plan mode writes plans, not answers
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-low"
    argv, _, _ = w.build_cmd({"cli": "agy"}, "s", "full", "/d", "p")
    assert argv[argv.index("--mode") + 1] == "accept-edits" and "--sandbox" not in argv


def test_worker_parses_cli_output_and_limits():
    import json
    w = _load_worker()
    out = json.dumps({"result": "pong", "is_error": False, "usage": {"input_tokens": 2, "cache_creation_input_tokens": 910,
                                                                    "cache_read_input_tokens": 0, "output_tokens": 4}})
    assert w.parse_output("claude", out, "", 0) == ("pong", 916, 0, None)
    out = json.dumps({"result": "pong", "usage": {"input_tokens": 3, "cache_read_input_tokens": 9000, "output_tokens": 4}})
    assert w.parse_output("claude", out, "", 0) == ("pong", 7, 9000, None)
    out = "\n".join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "pong"}},
        {"type": "turn.completed", "usage": {"input_tokens": 14833, "cached_input_tokens": 12160, "output_tokens": 5}}])
    assert w.parse_output("codex", out, "", 0) == ("pong", 2678, 12160, None)
    out = json.dumps({"status": "SUCCESS", "response": "pong\n", "usage": {"input_tokens": 8922, "total_tokens": 8923}})
    assert w.parse_output("agy", out, "", 0) == ("pong", 8923, 0, None)
    out = json.dumps({"status": "SUCCESS", "response": "pong", "usage": {"cache_read_tokens": 900, "total_tokens": 1000}})
    assert w.parse_output("agy", out, "", 0) == ("pong", 100, 900, None)
    out = json.dumps({"status": "ERROR", "error": {"message": "quota exceeded"}, "usage": {}})
    assert w.parse_output("agy", out, "", 1)[3] == "quota exceeded"
    text, _, _, err = w.parse_output("claude", json.dumps({"is_error": True, "result": "Claude AI usage limit reached|1767225600"}), "", 1)
    assert w.LIMIT_RE.search(err) and w.reset_time(err) == 1767225600.0
    assert w.parse_output("codex", "", "boom", 2)[3] == "boom"
    now = 1_000_000.0
    assert w.reset_time("You've hit your usage limit. Try again in 2 hours 5 minutes.", now) == now + 7500
    assert w.reset_time("quota exceeded", now) == now + w.COOLDOWN
    assert w.reset_time("limit reached, resets 3pm", now) > now


def test_worker_prompt_is_small_and_flat():
    w = _load_worker()
    job = {"id": 9, "room": "r", "sender": "sam", "text": "!deep why? " + "x" * 50, "room_summary": "talking",
           "parent": {"id": 8, "sender": "bob", "text": "y" * 50}}
    p = w.build_prompt(job, 30)
    assert "Room #r summary: talking" in p and "!deep" not in p and "#8 from bob" in p
    assert len(p) < 150


def test_worker_install_writes_a_private_env_and_a_user_service(tmp_path, monkeypatch):
    import os
    import stat
    w = _load_worker()
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(w.subprocess, "run", lambda argv, **kw: calls.append(argv) or type("R", (), {"returncode": 0})())
    w.install(["--hub", "http://hub.example:8700/", "--key", "bunchm_secret", "--max-power", "read", "--name", "bw"])
    env = tmp_path / ".config/bunch/bw.env"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    text = env.read_text()
    assert "BUNCH_URL=http://hub.example:8700\n" in text and "BUNCH_MACHINE_KEY=bunchm_secret" in text
    assert "WORKER_MAX_POWER=read" in text and "PATH=" in text  # services don't inherit ~/.local/bin
    unit = (tmp_path / ".config/systemd/user/bw.service").read_text()
    assert f"EnvironmentFile={env}" in unit and "worker.py" in unit and "WantedBy=default.target" in unit
    assert calls == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "bw"]]


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_files_upload_serve_and_purge(env):
    c, k, app, _ = env
    key = _machine(c)
    _on(c, "alice")
    c.post("/api/admin/rooms/pics/messages", json={"room": "pics", "text": "hi"}, headers=adm())
    up = lambda name="a.png", ct="image/png", data=PNG, room="pics": c.post(
        f"/api/v1/machine/file?agent=alice&room={room}&name={name}", content=data, headers={**h(key), "Content-Type": ct})
    r = up()
    assert r.status_code == 200 and r.json()["markdown"] == f"![a.png](file:{r.json()['id']})"
    fid = r.json()["id"]
    assert up(room="nope").status_code == 404 and up(data=b"").status_code == 413
    assert c.post("/api/v1/machine/file?agent=bob&room=pics", content=PNG, headers=h(key)).status_code == 403
    got = c.get(f"/api/admin/files/{fid}", headers=adm())
    assert got.content == PNG and got.headers["content-type"] == "image/png"
    assert c.get(f"/api/admin/files/{fid}").status_code == 401
    svg = up("x.svg", "image/svg+xml", b"<svg onload=alert(1)/>").json()["id"]  # an image, but sandboxed
    r = c.get(f"/api/admin/files/{svg}", headers=adm())
    assert r.headers["content-type"].startswith("image/svg+xml") and "sandbox" in r.headers["content-security-policy"]
    page = up("x.html", "text/html", b"<script>alert(1)</script>").json()["id"]  # never rendered inline
    r = c.get(f"/api/admin/files/{page}", headers=adm())
    assert r.headers["content-type"] == "application/octet-stream" and "attachment" in r.headers["content-disposition"]
    assert c.get("/api/admin/files", headers=h(k["alice"])).status_code in (401, 403)
    listing = c.get("/api/admin/files", headers=adm()).json()
    assert listing["rooms"][0]["n"] == 3 and len(listing["files"]) == 3
    assert c.post("/api/admin/files/purge", json={}, headers=adm()).status_code == 400
    assert c.post("/api/admin/files/purge", json={"ids": []}, headers=adm()).json()["deleted"] == 0
    uni = up("ハロウィン.pdf", "application/pdf", b"%PDF").json()["id"]  # non-Latin-1 name downloads
    r = c.get(f"/api/admin/files/{uni}", headers=adm())
    assert r.status_code == 200 and "filename*=UTF-8''%E3%83%8F" in r.headers["content-disposition"]
    c.post("/api/admin/files/purge", json={"ids": [uni]}, headers=adm())
    assert c.post("/api/admin/files/purge", json={"ids": [svg, page]}, headers=adm()).json()["deleted"] == 2
    assert c.put("/api/admin/files/retention", json={"days": 7}, headers=adm()).json()["retention_days"] == 7
    assert c.get(f"/api/admin/files/{fid}", headers=adm()).status_code == 200  # younger than 7 days
    assert c.post("/api/admin/files/purge", json={"older_than_days": 0}, headers=adm()).json()["deleted"] == 1
    assert c.get(f"/api/admin/files/{fid}", headers=adm()).status_code == 404
    fid = up().json()["id"]
    c.delete("/api/admin/rooms/pics", headers=adm())  # deleting a room takes its files along
    assert c.get(f"/api/admin/files/{fid}", headers=adm()).status_code == 404


def test_auto_agent_stays_quiet_when_model_returns_nothing_to_an_agent(env):
    from bunch.autoreply import autoreply_once
    c, k, app, _ = env
    hub = app.state.hub

    async def empty(*a, raw=False, **kw):
        return "" if raw else "(empty answer)"
    hub.ask = empty
    app.state.hub.store.x("UPDATE agents SET model='m' WHERE name='bob'")
    tool(c, k["alice"], "post", room="r", text="@bob hello", to="bob")
    asyncio.run(autoreply_once(hub))
    asyncio.run(autoreply_once(hub))
    assert not hub.store.q("SELECT 1 FROM messages WHERE sender='bob'")
    hub.post("human", "r", "@bob you there?", human=True)
    asyncio.run(autoreply_once(hub))
    assert "returned nothing" in hub.store.q("SELECT summary FROM messages WHERE sender='bob'")[0]["summary"]


def test_long_reply_summary_names_attachments_instead_of_cutting_them(env):
    c, k, app, _ = env
    hub = app.state.hub
    hub.post("alice", "r", "x" * 150 + "\n\n![halloween-1.png](file:12)")
    m = hub.store.q("SELECT * FROM messages ORDER BY id DESC LIMIT 1")[0]
    assert "file:" not in m["summary"] and "(file:12)" in m["body"]


def test_backup_copies_attachments(tmp_path):
    from bunch.store import Store
    st = Store(str(tmp_path / "live" / "bunch.db"))
    st.ensure_room("r")
    fid = st.add_file("r", "a.png", "image/png", PNG, "alice")
    st.backup(str(tmp_path / "bk.db"))
    assert (tmp_path / "bk.db.files" / str(fid)).read_bytes() == PNG


# --------------------------------------------------- findings from the 2026-10-28 user test
def test_mentions_match_whole_names_only():
    from bunch.hub import mentions
    w = _load_worker()
    for check in (mentions, w.mentions):
        assert check("@gemini, try again", "gemini") and check("ask @gemini.", "gemini")
        assert not check("@gemini-local tell them", "gemini") and not check("me@gemini", "gemini")


def test_worker_never_promises_pictures_it_cannot_make():
    import json
    w = _load_worker()
    assert "can't create files" in w.picture_hint("answer", "") and "never claim" in w.picture_hint("read", "/srv/x")
    assert "bunch-out" in w.picture_hint("full", "/srv/x")
    # agy "succeeds" with nothing to say when a tool it reached for was refused: say which.
    out = json.dumps({"status": "SUCCESS", "response": "", "usage": {"total_tokens": 15596},
                      "denied_actions": [{"action": "mcp", "display_name": "CallMcpTool"}]})
    text, tokens, _, err = w.parse_output("agy", out, "jetski: no output produced", 0)
    assert text == "" and tokens == 15596 and "CallMcpTool" in err
    assert w.parse_output("agy", json.dumps({"status": "SUCCESS", "response": ""}), "", 0)[3] == "(empty answer)"


def test_worker_rechecks_agents_before_dropping_a_job(monkeypatch):
    w = _load_worker()
    worker, posted, hellos = w.Worker(), [], []
    monkeypatch.setattr(worker, "hello", lambda force=False: hellos.append(force))
    monkeypatch.setattr(worker, "reply", lambda job, text: posted.append(text))
    worker.machine = "workstation"
    job = {"agent": "gemini", "id": 18, "room": "r", "sender": "sam", "human": True,
           "created": worker.started, "text": "@gemini hi"}
    worker.handle(job)
    assert hellos == [True] and posted == ["(gemini isn't set up on workstation yet; not answered)"]
    # Another agent's @gemini-local is not for gemini: no run, no reply.
    worker.handle({**job, "human": False, "sender": "gemini-local", "text": "@gemini-local again"})
    assert hellos == [True] and len(posted) == 1


def test_worker_says_when_power_was_lowered(monkeypatch):
    import json
    import subprocess
    w = _load_worker()
    worker, posted = w.Worker(), []
    worker.machine, worker.agents = "workstation", {"codex": {"cli": "claude", "power": "full", "dir": "/srv/x"}}
    monkeypatch.setattr(w, "report", lambda *a, **kw: "ok")
    monkeypatch.setattr(worker, "reply", lambda job, text: posted.append(text))
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": "no can do", "usage": {}}), "")
    monkeypatch.setattr(w.subprocess, "run", fake_run)
    worker.handle({"agent": "codex", "id": 37, "room": "r", "sender": "sam", "human": True,
                   "created": worker.started, "text": "@codex make a picture"})
    system = seen["argv"][seen["argv"].index("--system-prompt") + 1]
    assert "can't create files" in system and "plain text" in system
    assert posted[0].startswith("no can do") and "allows only answer, not full" in posted[0]


def test_router_sends_short_follow_ups_to_the_last_agent(env):
    c, k, app, llm = env
    hub = app.state.hub
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@bob make it"}, headers=adm())
    tool(c, k["bob"], "post", room="r", text="(agy failed: (empty answer))")

    async def picks_two(*a, **kw):
        raise AssertionError("a follow-up shouldn't need the router model")
    llm.chat = picks_two
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "Try again"}, headers=adm())
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["to"] == ["bob"]


def test_auto_agent_drops_copied_error_notes(env):
    from bunch.autoreply import FILLER_RE, answer
    c, k, app, _ = env
    hub = app.state.hub
    assert FILLER_RE.match("(empty answer)") and FILLER_RE.match("(agy failed: (empty answer))")
    assert not FILLER_RE.match("(I think so)")

    async def parrot(*a, raw=False, **kw):
        return "(empty answer)"
    hub.ask = parrot
    mid = int(hub.post("human", "r", "@bob try again", human=True).split("#")[1])
    bob = dict(hub.store.agent("bob"), model="m", system="")
    asyncio.run(answer(hub, bob, hub.store.message(mid)))
    assert "returned nothing" in hub.store.q("SELECT summary FROM messages WHERE sender='bob'")[0]["summary"]


def test_viewer_post_flags_unknown_mentions(env):
    c, k, app, llm = env
    r = c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "@claude hello, cc @bob"}, headers=adm())
    assert r.json()["unknown"] == ["claude"]


def test_message_summary_knows_who_spoke(env):
    c, k, app, llm = env
    hub = app.state.hub
    tool(c, k["bob"], "post", room="r", text="I can see #general. " + "x" * 400)
    asyncio.run(summarize_once(hub))
    msg = next(m for m in llm.calls if "20 words" in m[1][0]["content"])
    assert "third person" in msg[1][0]["content"] and msg[1][1]["content"].startswith("From bob:")


# --------------------------------------------------- gateway models from the viewer
class GatewayModels:
    """Stands in for LiteLLM's /model/* API: config-file models plus ones added at runtime."""
    def __init__(self, llm, db=True):
        self.deps = [{"name": "local/small", "id": "cfg1", "api_base": "http://192.0.2.7:11434",
                      "model": "ollama_chat/qwen2.5:3b", "stored": False}]
        self.db, self.fail_chat, self.n = db, set(), 0
        llm.model_info, llm.add_model, llm.delete_model = self.model_info, self.add_model, self.delete_model
        llm.models = self.models
        chat = llm.chat

        async def checked_chat(model, *a, **kw):
            if model in self.fail_chat:
                from bunch.llm import LLMError
                raise LLMError("gateway 500: connection refused")
            return await chat(model, *a, **kw)
        llm.chat = checked_chat

    async def models(self):
        return sorted({d["name"] for d in self.deps} | {"paid/big"})

    async def model_info(self):
        return [dict(d) for d in self.deps]

    async def add_model(self, name, model, api_base=None):
        from bunch.llm import LLMError, NO_DB
        if not self.db:
            raise LLMError(NO_DB)
        self.n += 1
        self.deps.append({"name": name, "id": f"db{self.n}", "api_base": api_base or "", "model": model, "stored": True})

    async def delete_model(self, mid):
        self.deps = [d for d in self.deps if d["id"] != mid]


def test_admin_adds_tests_renames_and_removes_models(env):
    c, k, app, llm = env
    gw = GatewayModels(llm)
    add = lambda **b: c.post("/api/admin/models", json={"name": "local/coder", "model": "ollama_chat/qwen2.5-coder:14b",
                                                         "api_base": "http://gpu.example.com:11434", **b}, headers=adm())
    assert c.post("/api/admin/models", json={"name": "x", "model": "y"}, headers=h(k["alice"])).status_code in (401, 403)
    assert add(name="bad name").status_code == 400 and add(api_base="gpu:11434").status_code == 400
    assert add(name="local/small").status_code == 409  # already there
    gw.fail_chat.add("local/coder")
    r = add()
    assert r.status_code == 502 and "OLLAMA_HOST" in r.json()["detail"]
    assert "local/coder" not in {d["name"] for d in gw.deps}  # an unreachable model isn't kept
    gw.fail_chat.clear()
    assert add().status_code == 200
    models = {m["name"]: m for m in c.get("/api/admin/models", headers=adm()).json()}
    assert models["local/coder"]["stored"] and not models["local/small"]["stored"]
    assert c.post("/api/admin/models/test", json={"name": "local/coder"}, headers=adm()).json()["ok"]
    # config-file models are changed in config.yaml, not here
    assert c.post("/api/admin/models/delete", json={"name": "local/small"}, headers=adm()).status_code == 400
    c.post("/api/admin/agents", json={"name": "coder", "model": "local/coder"}, headers=adm())
    r = c.post("/api/admin/models/rename", json={"name": "local/coder", "to": "local/code"}, headers=adm())
    assert r.status_code == 200 and r.json()["agents_moved"] == ["coder"]
    assert {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}["coder"]["model"] == "local/code"
    assert c.post("/api/admin/models/delete", json={"name": "local/code"}, headers=adm()).status_code == 409  # in use
    r = c.post("/api/admin/models/delete", json={"name": "local/code", "force": True}, headers=adm())
    assert r.status_code == 200 and r.json()["used_by"] == ["coder"] and "local/code" not in {d["name"] for d in gw.deps}
    # the summarizer's model is set in .env: renaming it here would break it
    gw.deps.append({"name": "local/small", "id": "db9", "api_base": "", "model": "x", "stored": True})
    assert c.post("/api/admin/models/rename", json={"name": "local/small", "to": "local/s"}, headers=adm()).status_code == 409


def test_adding_models_without_gateway_database_says_how(env):
    c, k, app, llm = env
    GatewayModels(llm, db=False)
    r = c.post("/api/admin/models", json={"name": "local/coder", "model": "ollama_chat/x"}, headers=adm())
    assert r.status_code == 502 and "STORE_MODEL_IN_DB" in r.json()["detail"]


def test_llm_maps_no_database_error():
    import httpx
    from bunch.llm import LLM, LLMError, NO_DB
    llm = LLM("http://gw", "k")
    orig = httpx.AsyncClient

    def client(**kw):
        return orig(transport=httpx.MockTransport(lambda req: httpx.Response(
            500, json={"error": {"message": "No DB Connected. Set STORE_MODEL_IN_DB=True"}})), **kw)
    httpx.AsyncClient = client
    try:
        with pytest.raises(LLMError) as e:
            asyncio.run(llm.add_model("local/x", "ollama_chat/x", "http://h:11434"))
    finally:
        httpx.AsyncClient = orig
    assert str(e.value) == NO_DB


def test_machine_shares_ollama_models_and_spots_gone_ones(env):
    c, k, app, llm = env
    gw = GatewayModels(llm)
    key = _machine(c)
    hello = lambda o: c.post("/api/v1/machine/hello", json={"hostname": "workstation", "addresses": ["192.0.2.7", "workstation"],
                                                            "clis": CLIS, "ollama": o}, headers=h(key))
    m = c.get("/api/admin/machines", headers=adm()).json()[0]
    assert m["ollama"] is None  # not shared unless the machine opts in
    hello({"port": 11434, "models": ["qwen2.5:3b", "llama3.1:8b"]})
    m = c.get("/api/admin/machines", headers=adm()).json()[0]
    assert m["ollama"] == {"url": "http://workstation:11434", "models": ["llama3.1:8b", "qwen2.5:3b"]}
    assert c.get("/api/admin/machines", headers=h(k["alice"])).status_code in (401, 403) or \
        "ollama" not in c.get("/api/admin/machines", headers=h(k["alice"])).json()[0]
    gw.deps.append({"name": "local/ws-llama", "id": "db1", "api_base": "http://workstation:11434",
                    "model": "ollama_chat/llama3.1:8b", "stored": True})
    gone = lambda: {x["name"]: x["gone"] for x in c.get("/api/admin/models", headers=adm()).json()}["local/ws-llama"]
    assert gone() is False
    hello({"port": 11434, "models": ["qwen2.5:3b"]})  # ollama rm llama3.1:8b on that machine
    assert gone() is True


def test_viewer_sees_power_the_machine_actually_allows(env):
    c, k, app, llm = env
    _machine(c)  # max_power read, dirs /srv/app
    _on(c, "alice", power="full", dir="/srv/app")
    _on(c, "bob", power="read", dir="/srv/other")
    _on(c, "carol", power="read", dir="/srv/app/sub")
    agents = {a["name"]: a for a in c.get("/api/admin/agents", headers=adm()).json()}
    assert agents["alice"]["runs_as"] == "read" and agents["bob"]["runs_as"] == "answer"
    assert agents["carol"]["runs_as"] == "read"


def test_worker_flags_pictures_it_never_made():
    w = _load_worker()
    assert w.claims_picture("Created a sleepy watercolor fox. 🦊")
    assert w.claims_picture("I've generated the image for you.")
    assert w.claims_picture("Saved it as fox.png")
    assert not w.claims_picture("I can't create images in this run.")
    assert not w.claims_picture("Here is how to resize an image with PIL.")
    note = w.no_picture_note("workstation", "answer", max_power="answer", dirs=[])
    assert "ran this as answer" in note and "WORKER_MAX_POWER=full" in note
    assert "WORKER_MAX_POWER" not in w.no_picture_note("ws", "read", max_power="full", dirs=["/srv"])
    assert "nothing was saved" in w.no_picture_note("ws", "full")
    assert ".svg" in w.IMAGE_TYPES
    assert "mcp" in w.empty_hint("agy", "answer")


def test_worker_reads_ollama_tags(tmp_path):
    import http.server
    import json
    import threading
    w = _load_worker()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": [{"name": "qwen2.5:3b"}, {"model": "llama3.1:8b"}]}).encode()
            self.send_response(200 if self.path == "/api/tags" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert w.ollama_models(f"http://127.0.0.1:{port}") == {"port": port, "models": ["llama3.1:8b", "qwen2.5:3b"]}
    finally:
        srv.shutdown()
    assert w.ollama_models("http://127.0.0.1:9") is None  # nothing listening


def test_worker_takes_agy_pictures_only_from_its_own_conversation(tmp_path):
    import json
    import time
    w = _load_worker()
    since = time.time() - 1
    for cid in ("1d55a2b5-2bbf", "other-conv"):
        (tmp_path / cid).mkdir()
        (tmp_path / cid / "fox.jpg").write_bytes(b"x")
    out = json.dumps({"conversation_id": "1d55a2b5-2bbf", "response": "Generating image..."})
    assert w.agy_images(out, since, str(tmp_path)) == [str(tmp_path / "1d55a2b5-2bbf" / "fox.jpg")]
    assert w.agy_images(json.dumps({"conversation_id": "../other-conv"}), since, str(tmp_path)) == []
    assert w.agy_images("not json", since, str(tmp_path)) == []
    assert w.claims_picture("Created ./bunch-out/moon-cat.png using built-in image generation.")


def test_cached_tokens_count_a_tenth_toward_the_budget(env):
    c, k, app, llm = env
    key = _machine(c)
    _on(c, "alice")
    rep = lambda **b: c.post("/api/v1/machine/report", json={"agent": "alice", "status": "online", **b}, headers=h(key))
    assert rep(tokens=10, calls=1, cached=1000).text == "ok 110/250"
    assert rep(tokens=5, calls=1).text == "ok 115/250"  # an older worker sends no cached count
    a = {x["name"]: x for x in c.get("/api/admin/agents", headers=adm()).json()}["alice"]
    assert (a["sub_tokens_today"], a["sub_cached_today"], a["sub_calls_today"], a["spent_today"]) == (15, 1000, 2, 115)
    assert rep(tokens=0, calls=1, cached=1400).text == "over budget 255/250"
