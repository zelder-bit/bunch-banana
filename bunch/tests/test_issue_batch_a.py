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
