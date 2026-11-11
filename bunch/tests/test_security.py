"""Security hardening: admin-token rate limit, session secret, per-account lockout, size limits,
attachment quota, MCP batch cap, and token sign-in for the viewer (cookie session, security headers)."""
import logging

import pytest
from fastapi.testclient import TestClient

from bunch.app import create_app
from bunch.config import Settings
from tests.test_bunch import ADMIN, PNG, FakeLLM, _machine, _on, adm, h


def make(tmp_path, **kw):
    kw.setdefault("admin_token", ADMIN)
    s = Settings(db_path=str(tmp_path / "hub.db"), litellm_url="http://x", **kw)
    return create_app(s, FakeLLM())


class FromIP:
    """Test-only wrapper: the client address of each request is whatever its X-Test-IP header says."""

    def __init__(self, app):
        self.app = app

    def __getattr__(self, name):
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            ip = dict(scope["headers"]).get(b"x-test-ip", b"127.0.0.1").decode()
            scope = {**scope, "client": (ip, 5000)}
        await self.app(scope, receive, send)


def at(app, i):
    """A client that appears to come from 10.0.0.<i>."""
    return TestClient(FromIP(app), headers={"X-Test-IP": f"10.0.0.{i}"})


@pytest.fixture
def hub(tmp_path):
    """A fresh app per test (rate-limit counts live in the app), plus an agent key for alice."""
    app = make(tmp_path)
    with TestClient(app) as c:
        key = c.post("/api/admin/agents", json={"name": "alice"}, headers=adm()).json()["key"]
        yield c, key, app


def burn_bad_tokens(c, n=10, path="/api/admin/rooms"):
    for _ in range(n):
        assert c.get(path, headers=h("not-the-token")).status_code == 401


# ---------------------------------------------------------------- #59
def test_wrong_admin_tokens_are_rate_limited(hub):
    c, key, _ = hub
    assert c.get("/api/admin/rooms", headers=adm()).status_code == 200  # right token works before the limit
    burn_bad_tokens(c)
    assert c.get("/api/admin/rooms", headers=h("not-the-token")).status_code == 429
    # over the limit the token isn't even looked at, so the right one is refused too, on any viewer route
    assert c.get("/api/admin/rooms", headers=adm()).status_code == 429
    assert c.get("/api/auth/me", headers=adm()).status_code == 429
    # agent keys and machine keys use other routes and are untouched
    assert c.get("/api/v1/rooms", headers=h(key)).status_code == 200
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=h(key)).status_code == 200
    # no Authorization header at all is just "not signed in", not a failure and not a 429
    assert c.get("/api/admin/rooms").status_code == 401


def test_session_cookie_user_unaffected_by_token_guessing(hub):
    _, _, app = hub
    app.state.hub.store.create_user("pat", "correct horse")
    pat, guesser = at(app, 7), at(app, 7)   # same address, different browsers
    assert pat.post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 200
    burn_bad_tokens(guesser)
    assert guesser.get("/api/admin/rooms", headers=h("not-the-token")).status_code == 429
    assert pat.get("/api/admin/rooms").status_code == 200  # cookie, no Bearer: not affected


def test_ws_first_frame_token_is_rate_limited(hub):
    c, *_ = hub
    for _ in range(10):
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"token": "nope"})
            assert ws.receive()["code"] == 4401
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})   # the right token, but this address is over the limit
        msg = ws.receive()
        assert msg["type"] == "websocket.close" and msg["code"] == 1008


def test_ws_correct_token_still_works(hub):
    c, *_ = hub
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"token": ADMIN})
        assert ws.receive_json()["type"] == "ready"


def test_session_secret_is_random_persisted_and_not_from_the_token(tmp_path):
    a1 = make(tmp_path, admin_token="first-admin-token")
    secret = a1.state.hub.store.setting("session_secret")
    assert len(secret) == 64 and "first-admin-token" not in secret
    a2 = make(tmp_path, admin_token="second-admin-token")   # same database, different token
    assert a2.state.hub.store.setting("session_secret") == secret
    # a user's cookie from the first start is still good after a restart
    a1.state.hub.store.create_user("pat", "correct horse")
    with TestClient(a1) as c1:
        assert c1.post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 200
        with TestClient(a2) as c2:
            c2.cookies.set("bunch_session", c1.cookies["bunch_session"])
            assert c2.get("/api/auth/me").json()["name"] == "pat"
    # a different database gets a different secret
    other = tmp_path / "other"
    other.mkdir()
    assert make(other).state.hub.store.setting("session_secret") != secret


def test_configured_session_secret_is_used_not_stored(tmp_path):
    app = make(tmp_path, session_secret="from-env")
    assert app.state.hub.store.setting("session_secret") == ""


def test_weak_admin_token_warns_but_starts(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bunch"):
        make(tmp_path, admin_token="short")
    assert any("openssl rand -hex 32" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="bunch"):
        make(tmp_path, admin_token="x" * 32)
    assert not caplog.records


# ---------------------------------------------------------------- #60
def test_per_account_lockout_from_many_addresses(tmp_path):
    app = make(tmp_path)
    app.state.hub.store.create_user("pat", "correct horse")
    clients = [at(app, i) for i in range(1, 7)]
    n = 0
    for c in clients[:5]:   # 5 addresses x 2 wrong passwords: no address is near its own limit
        for name in ("pat", " PAT "):   # case and spaces don't make a new account
            assert c.post("/api/auth/login", json={"name": name, "password": "wrong wrong"}).status_code == 401
            n += 1
    fresh = clients[5]
    assert n == 10
    assert fresh.post("/api/auth/login", json={"name": "pat", "password": "wrong wrong"}).status_code == 429
    assert fresh.post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 429
    # other accounts and unknown names are not locked by it
    assert fresh.post("/api/auth/login", json={"name": "someone", "password": "x"}).status_code == 401


def test_success_clears_the_account_counter(tmp_path):
    app = make(tmp_path)
    app.state.hub.store.create_user("pat", "correct horse")
    clients = [at(app, i) for i in range(1, 6)]
    for c in clients[:4]:
        for _ in range(2):
            c.post("/api/auth/login", json={"name": "pat", "password": "wrong wrong"})
    assert clients[4].post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 200
    # 8 failures were wiped by the success, so a new address has all its tries again
    other = at(app, 99)
    for _ in range(9):
        assert other.post("/api/auth/login", json={"name": "pat", "password": "wrong wrong"}).status_code == 401


def test_prove_password_counts_against_the_account(tmp_path):
    app = make(tmp_path)
    st = app.state.hub.store
    st.create_user("boss", "correct horse", admin=True)
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"name": "boss", "password": "correct horse"}).status_code == 200
        for i in range(10):   # ten wrong "your password" proofs
            r = c.post("/api/admin/users", json={"name": f"u{i}", "password": "long enough", "admin": True,
                                                 "my_password": "wrong wrong"})
            assert r.status_code == 403
        assert c.post("/api/admin/users", json={"name": "ok", "password": "long enough", "admin": True,
                                                "my_password": "correct horse"}).status_code == 429


def test_huge_account_names_do_not_bloat_the_counter(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"name": "n" * 50_000, "password": "x"}).status_code == 401
    # the key is capped: two names that differ only past the cap are the same account
    from bunch.app import _name_key
    assert len(_name_key("n" * 50_000)) == 64 and _name_key("  Pat ") == "pat"


# ---------------------------------------------------------------- #61
def test_oversized_content_length_is_413(tmp_path):
    app = make(tmp_path, max_body=1000, max_request=5000)   # limit = max(5000, 4*1000+10000) = 14000
    with TestClient(app) as c:
        r = c.post("/api/auth/login", content=b"x" * 20_000, headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        ok = c.post("/api/auth/login", json={"name": "x", "password": "y"})
        assert ok.status_code == 401   # a normal request still reaches the app


def test_chunked_body_without_content_length_is_cut_off(tmp_path):
    app = make(tmp_path, max_body=1000, max_request=5000)

    def chunks():
        for _ in range(40):
            yield b"x" * 1000

    with TestClient(app) as c:
        req = c.build_request("POST", "/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
        assert "content-length" not in req.headers
        assert c.send(req).status_code == 413


def test_max_body_message_fits_under_the_default_limit(hub):
    c, *_ = hub
    text = "é" * 200_000   # max_body chars, 2 bytes each
    r = c.post("/api/admin/rooms/general/messages", json={"room": "general", "text": text}, headers=adm())
    assert r.status_code == 200


def test_upload_over_max_file_is_413(tmp_path):
    app = make(tmp_path, max_file=1000)
    with TestClient(app) as c:
        key = _machine(c)
        c.post("/api/admin/agents", json={"name": "alice"}, headers=adm())
        _on(c, "alice")
        c.post("/api/admin/rooms/pics/messages", json={"room": "pics", "text": "hi"}, headers=adm())
        up = lambda n: c.post("/api/v1/machine/file?agent=alice&room=pics", content=b"0" * n, headers=h(key))
        assert up(1000).status_code == 200
        assert up(1001).status_code == 413            # caught by the file check
        r = up(1000 + 100_000)                        # refused by the size middleware before it is read
        assert r.status_code == 413


def test_attachment_quota(tmp_path):
    app = make(tmp_path, max_files_total=100)
    with TestClient(app) as c:
        c.post("/api/admin/agents", json={"name": "alice"}, headers=adm())
        key = _machine(c)
        _on(c, "alice")
        c.post("/api/admin/rooms/pics/messages", json={"room": "pics", "text": "hi"}, headers=adm())
        up = lambda: c.post("/api/v1/machine/file?agent=alice&room=pics", content=PNG, headers=h(key))
        first, second = up(), up()
        assert first.status_code == 200 and second.status_code == 200   # 2 x 40 bytes
        full = up()
        assert full.status_code == 413 and "storage is full" in full.text and "Files" in full.text
        store = app.state.hub.store
        assert store.files_total() == 2 * len(PNG)
        store.delete_files(ids=[first.json()["id"]])
        assert up().status_code == 200   # freed space is usable again


def test_quota_zero_is_unlimited(tmp_path):
    app = make(tmp_path, max_files_total=0)
    with TestClient(app) as c:
        c.post("/api/admin/agents", json={"name": "alice"}, headers=adm())
        key = _machine(c)
        _on(c, "alice")
        c.post("/api/admin/rooms/pics/messages", json={"room": "pics", "text": "hi"}, headers=adm())
        for _ in range(3):
            assert c.post("/api/v1/machine/file?agent=alice&room=pics", content=PNG, headers=h(key)).status_code == 200


def test_mcp_batch_cap(hub):
    c, key, _ = hub
    ping = lambda i: {"jsonrpc": "2.0", "id": i, "method": "ping"}
    ok = c.post("/mcp", json=[ping(i) for i in range(50)], headers=h(key))
    assert ok.status_code == 200 and len(ok.json()) == 50
    big = c.post("/mcp", json=[ping(i) for i in range(51)], headers=h(key))
    assert big.status_code == 200 and big.json()["error"]["code"] == -32600


# ---------------------------------------------------------------- #72
def test_token_login_gives_a_cookie_session(hub):
    c, _, app = hub
    st = app.state.hub.store
    st.create_user("pat", "correct horse")
    assert c.get("/api/admin/agents").status_code == 401
    r = c.post("/api/auth/token", json={"token": f" {ADMIN} "})
    assert r.status_code == 200 and r.json() == {"name": "human", "admin": True}
    assert "bunch_session" in c.cookies and ADMIN not in c.cookies["bunch_session"]
    # no Authorization header from here on
    me = c.get("/api/auth/me").json()
    assert me["name"] == "human" and me["admin"] is True and me["token"] is True
    assert c.get("/api/admin/users").status_code == 200   # an admin-only route
    assert c.post("/api/admin/rooms/pics/messages", json={"room": "pics", "text": "hi"}).status_code == 200
    # attachments are plain URLs: the cookie alone must fetch them
    key = _machine(c)
    c.post("/api/admin/agents", json={"name": "alice"})
    assert c.put("/api/admin/agents/alice/worker", json={"mode": "worker", "machine": "workstation", "cli": "claude"}).status_code == 200
    fid = c.post("/api/v1/machine/file?agent=alice&room=pics", content=PNG, headers=h(key)).json()["id"]
    got = c.get(f"/api/admin/files/{fid}")
    assert got.status_code == 200 and got.content == PNG
    with c.websocket_connect("/ws") as ws:
        ws.send_json({})
        assert ws.receive_json()["type"] == "ready"
    # sign out ends it
    c.post("/api/auth/logout")
    assert c.get("/api/admin/users").status_code == 401
    assert c.get(f"/api/admin/files/{fid}").status_code == 401


def test_token_login_wrong_token_and_rate_limit(hub):
    c, *_ = hub
    for _ in range(10):
        assert c.post("/api/auth/token", json={"token": "wrong"}).status_code == 401
    assert "bunch_session" not in c.cookies
    assert c.post("/api/auth/token", json={"token": ADMIN}).status_code == 429   # over the limit
    assert c.post("/api/auth/token", json={"token": ""}).status_code in (401, 429)


def test_token_login_failures_share_the_bearer_budget(hub):
    c, *_ = hub
    burn_bad_tokens(c, 5)
    for _ in range(5):
        assert c.post("/api/auth/token", json={"token": "wrong"}).status_code == 401
    assert c.get("/api/admin/rooms", headers=adm()).status_code == 429


def test_changing_the_admin_token_invalidates_token_sessions(tmp_path):
    a1 = make(tmp_path, admin_token="the-first-admin-token")
    with TestClient(a1) as c1:
        assert c1.post("/api/auth/token", json={"token": "the-first-admin-token"}).status_code == 200
        assert c1.get("/api/admin/users").status_code == 200
        cookie = c1.cookies["bunch_session"]
    a2 = make(tmp_path, admin_token="the-second-admin-token")   # same database and cookie key, new token
    with TestClient(a2) as c2:
        c2.cookies.set("bunch_session", cookie)
        assert c2.get("/api/admin/users").status_code == 401
        assert c2.get("/api/auth/me").status_code == 401


def test_token_session_is_as_strong_as_the_bearer_token(hub):
    c, _, app = hub
    st = app.state.hub.store
    c.post("/api/auth/token", json={"token": ADMIN})
    # admin_proves_it: making an admin needs no password from the token, and no my_password here
    assert c.post("/api/admin/users", json={"name": "sam", "password": "long enough", "admin": True}).status_code == 200
    assert c.put("/api/admin/users/sam/password", json={"password": "another long one"}).status_code == 200
    # keep_one: the token can remove the last admin, as it could with the header
    assert c.put("/api/admin/users/sam/admin", json={"admin": False}).status_code == 200
    assert c.put("/api/admin/users/sam/admin", json={"admin": True}).status_code == 200
    assert c.delete("/api/admin/users/sam").status_code == 200
    assert st.agent("human") is not None


def test_password_admin_session_still_needs_its_password(hub):
    c, _, app = hub
    app.state.hub.store.create_user("boss", "correct horse", admin=True)
    c.post("/api/auth/login", json={"name": "boss", "password": "correct horse"})
    assert c.post("/api/admin/users", json={"name": "sam", "password": "long enough", "admin": True}).status_code == 403
    assert c.post("/api/admin/users", json={"name": "sam", "password": "long enough", "admin": True,
                                            "my_password": "correct horse"}).status_code == 200
    assert c.get("/api/auth/me").json()["token"] is False
    # the last admin can't be removed from a session
    assert c.request("DELETE", "/api/admin/users/sam", json={"my_password": "correct horse"}).status_code == 200
    r = c.request("DELETE", "/api/admin/users/boss", json={"my_password": "correct horse"})
    assert r.status_code == 400


def test_password_login_replaces_a_token_session(hub):
    c, _, app = hub
    app.state.hub.store.create_user("pat", "correct horse")
    c.post("/api/auth/token", json={"token": ADMIN})
    c.post("/api/auth/login", json={"name": "pat", "password": "correct horse"})
    me = c.get("/api/auth/me").json()
    assert me["name"] == "pat" and me["admin"] is False and me["token"] is False


def test_forged_token_marker_is_rejected(hub):
    c, *_ = hub
    # the cookie is signed, so a client can't make its own; also the marker isn't a bare hash of the token
    c.cookies.set("bunch_session", "eyJ0b2tlbl9hZG1pbiI6ICJ4In0=.AAAA.BBBB")
    assert c.get("/api/admin/users").status_code == 401


def test_index_has_security_headers(hub):
    c, *_ = hub
    r = c.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp
    assert "default-src 'self'" in csp
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "<title>" in r.text


def test_viewer_no_longer_keeps_the_token():
    from pathlib import Path
    page = (Path(__file__).resolve().parents[1] / "bunch" / "static" / "index.html").read_text()
    assert 'store.set("bunch.token"' not in page and "Bearer" not in page
    assert 'store.del("bunch.token")' in page and "/api/auth/token" in page
