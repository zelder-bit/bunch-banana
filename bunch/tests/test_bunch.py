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
    r = c.post("/api/admin/rooms/loop/messages", json={"room": "loop", "text": "carry on"}, headers=adm())
    assert r.status_code == 200
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
