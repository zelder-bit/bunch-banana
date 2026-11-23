"""C1: the hub keeps each person's read position per room and computes the viewer's unread / mention counts."""
from fastapi.testclient import TestClient

from bunch.store import Store

from .test_bunch import ADMIN, adm, env, h  # noqa: F401  (env is a fixture)


def counts(c, **kw):
    return {r["name"]: (r["unread"], r["mention"]) for r in c.get("/api/admin/rooms", **kw).json()}


def agent_says(c, key, room, text):
    r = c.post("/api/v1/post", json={"room": room, "text": text}, headers=h(key))
    assert r.status_code == 200, r.text


def newest(c, room):
    return c.get(f"/api/admin/rooms/{room}/messages", headers=adm()).json()[-1]["id"]


def read(c, room, mid, **kw):
    return c.post(f"/api/admin/rooms/{room}/read", json={"id": mid}, **kw)


def test_position_moves_forward_only(env):
    c, keys, app, _ = env
    st = app.state.hub.store
    agent_says(c, keys["alice"], "r", "one")
    first = newest(c, "r")
    agent_says(c, keys["alice"], "r", "two")
    second = newest(c, "r")
    assert counts(c, headers=adm())["r"] == (2, 0)
    assert read(c, "r", second, headers=adm()).json() == {"room": "r", "read": second}
    assert read(c, "r", first, headers=adm()).json()["read"] == second  # never back
    assert read(c, "r", second + 1000, headers=adm()).json()["read"] == second  # not past the room's end
    assert counts(c, headers=adm())["r"] == (0, 0)
    assert read(c, "nope", 1, headers=adm()).status_code == 404
    assert read(c, "r", 1).status_code == 401
    # the person's cursor row leaves agents' state alone
    assert st.cursor("alice", "r") is not None and "r" not in st.joined_rooms("bob")


def test_counts_per_person_and_mentions(env):
    c, keys, app, _ = env
    st = app.state.hub.store
    st.create_user("pat", "correct horse")
    agent_says(c, keys["alice"], "r", "hello all")
    agent_says(c, keys["alice"], "r", "@pat a question for you")
    pat = TestClient(app)
    assert pat.post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 200
    assert counts(pat)["r"] == (2, 1)
    assert counts(c, headers=adm())["r"] == (2, 0)  # the admin-token person isn't @pat
    read(pat, "r", newest(c, "r"))
    assert counts(pat)["r"] == (0, 0)
    assert counts(c, headers=adm())["r"] == (2, 0)  # two people differ
    pat.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "my own post"})
    assert counts(pat)["r"] == (0, 0)  # what you post yourself isn't unread
    assert counts(c, headers=adm())["r"] == (3, 0)


def test_counts_survive_a_new_session(env):
    c, keys, app, _ = env
    app.state.hub.store.create_user("pat", "correct horse")
    agent_says(c, keys["alice"], "a", "in a")
    agent_says(c, keys["alice"], "b", "in b")

    def device():
        d = TestClient(app)
        assert d.post("/api/auth/login", json={"name": "pat", "password": "correct horse"}).status_code == 200
        return d

    one = device()
    read(one, "a", newest(c, "a"))
    two = device()  # a second device, or the same one after a reload
    assert counts(two) == counts(one) == {"a": (0, 0), "b": (1, 0)}
    # the admin token: the bearer header and a session made from it are the same person
    read(c, "b", newest(c, "b"), headers=adm())
    tok = TestClient(app)
    assert tok.post("/api/auth/token", json={"token": ADMIN}).status_code == 200
    assert counts(tok)["b"] == (0, 0)


def test_new_person_starts_with_nothing_unread(env):
    c, keys, app, _ = env
    agent_says(c, keys["alice"], "r", "@pat before pat existed")
    app.state.hub.store.create_user("pat", "correct horse")
    pat = TestClient(app)
    pat.post("/api/auth/login", json={"name": "pat", "password": "correct horse"})
    assert counts(pat)["r"] == (0, 0)
    agent_says(c, keys["alice"], "r", "@pat now")
    assert counts(pat)["r"] == (1, 1)


def test_upgrade_starts_existing_people_with_nothing_unread(tmp_path):
    path = str(tmp_path / "old.db")
    st = Store(path)
    st.create_user("pat", "correct horse")
    st.create_agent("alice")
    st.ensure_room("r")
    st.add_message("r", "alice", False, ["pat"], "old, to pat", None, None, False)
    st.add_message("r", "alice", False, [], "old, to all", None, None, False)
    # as it was before migration 5: version 4, everyone's first_id 0
    st.db.execute("UPDATE agents SET first_id=0")
    st.db.execute("PRAGMA user_version=4")
    st.db.close()
    st = Store(path)
    assert st.read_counts("pat") == {}
    assert st.agent_first_id("alice") == 0  # agents keep theirs
    st.add_message("r", "alice", False, ["pat"], "new", None, None, False)
    assert st.read_counts("pat") == {"r": (1, 1)}
