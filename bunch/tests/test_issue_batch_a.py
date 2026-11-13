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
