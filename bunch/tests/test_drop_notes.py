"""A3: every place a message is dropped leaves one "(...; not answered)" note, or is routed again."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from bunch import autoreply
from bunch.app import create_app
from bunch.config import Settings
from bunch.hub import HUB_SENDER, MAX_OFFERS, REDELIVER_WINDOW, post_id
from .test_bunch import ADMIN, FakeLLM, _machine, _on

ADM = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def env(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "hub.db"), admin_token=ADMIN), FakeLLM())
    with TestClient(app) as c:
        for n in ("alice", "bob"):
            assert c.post("/api/admin/agents", json={"name": n}, headers=ADM).status_code == 200
        yield c, app.state.hub


def notes(hub, mid, sender):
    return [r["summary"] for r in hub.store.q("SELECT summary FROM messages WHERE reply_to=? AND sender=?",
                                               (mid, sender))]


def test_both_offers_unanswered_leaves_a_note_at_the_third(env):
    c, hub = env
    mid = post_id(hub.post("sam", "r", "@alice hello?", human=True))
    for _ in range(MAX_OFFERS):
        assert [r["id"] for r in hub.jobs("alice")] == [mid]
        assert notes(hub, mid, "alice") == []
    assert hub.jobs("alice") == []
    assert notes(hub, mid, "alice") == [f"(alice was handed this {MAX_OFFERS} times without answering; not answered)"]


def test_redeliver_window_running_out_leaves_a_note(env):
    c, hub = env
    mid = post_id(hub.post("sam", "r", "@alice hello?", human=True))
    assert [r["id"] for r in hub.jobs("alice")] == [mid]
    hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - REDELIVER_WINDOW - 1, mid))
    assert hub.jobs("alice") == []
    assert notes(hub, mid, "alice") == [f"(alice did not answer within {REDELIVER_WINDOW // 60} minutes; not answered)"]


def test_an_answered_job_gets_no_note(env):
    c, hub = env
    mid = post_id(hub.post("sam", "r", "@alice hello?", human=True))
    hub.jobs("alice")
    hub.post("alice", "r", "hi", reply_to=mid)
    hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - REDELIVER_WINDOW - 1, mid))
    hub.jobs("alice")
    assert notes(hub, mid, "alice") == ["hi"]


def test_a_note_is_posted_once_per_message(env):
    c, hub = env
    mid = post_id(hub.post("sam", "r", "@alice hello?", human=True))
    for _ in range(MAX_OFFERS + 3):
        hub.jobs("alice")
    hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - REDELIVER_WINDOW - 1, mid))
    hub.offered.clear()  # even when the hub forgets what it offered (e.g. the 5000-entry reset)
    hub.offered[("alice", mid)] = 1
    for _ in range(3):
        hub.jobs("alice")
    assert len(notes(hub, mid, "alice")) == 1


def test_bot_handoff_to_a_missing_agent_is_noted_and_pauses(env):
    c, hub = env
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "kickoff"}, headers=ADM)
    assert c.put("/api/admin/rooms/b/bot", json={"on": True}, headers=ADM).status_code == 200
    goal = post_id(hub.post("human", "b", "@bob plan it", human=True))
    mid = post_id(hub.post("bob", "b", "Plan drafted.\n@ghost, can you price it?", reply_to=goal))
    m = hub.store.message(mid)
    assert notes(hub, mid, HUB_SENDER) == ["(bot mode: there is no agent named ghost, so nobody was asked; "
                                           "not answered)"]
    assert not any(hub.wakes(m, n) for n in ("alice", "bob"))
    assert not hub.run_live("b")
    hub._bot_turn(mid)  # looked at again: still one note
    assert len(notes(hub, mid, HUB_SENDER)) == 1


def test_bot_handoff_to_a_real_agent_gets_no_note(env):
    c, hub = env
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "kickoff"}, headers=ADM)
    assert c.put("/api/admin/rooms/b/bot", json={"on": True}, headers=ADM).status_code == 200
    goal = post_id(hub.post("human", "b", "@bob plan it", human=True))
    mid = post_id(hub.post("bob", "b", "Plan drafted.\n@ghost and @alice, can you price it?", reply_to=goal))
    assert notes(hub, mid, HUB_SENDER) == []
    assert hub.wakes(hub.store.message(mid), "alice")


def test_empty_auto_answer_to_an_agent_is_noted(env, monkeypatch):
    c, hub = env
    hub.store.create_agent("helper", model="local/small")

    async def empty(*a, **kw):
        return "", 10
    monkeypatch.setattr(hub.llm, "chat", empty)
    mid = post_id(hub.post("alice", "r", "@helper can you check this?"))
    asyncio.run(autoreply.autoreply_once(hub))
    asyncio.run(autoreply.autoreply_once(hub))  # another pass: no second note, no second ask
    assert notes(hub, mid, "helper") == ["(local/small returned nothing; not answered)"]


def test_startup_routes_recent_unaddressed_human_posts_again(tmp_path):
    s = Settings(db_path=str(tmp_path / "hub.db"), admin_token=ADMIN)
    with TestClient(create_app(s, FakeLLM())) as c:
        assert c.post("/api/admin/agents", json={"name": "alice"}, headers=ADM).status_code == 200
        st = c.app.state.hub.store
        st.set_setting("first_responder", "alice")
        # as if the hub went down while routing these: stored with nobody chosen yet
        fresh = st.add_message("r", "sam", True, [], "anyone there?", None, None, False)
        old = st.add_message("r", "sam", True, [], "older", None, None, False)
        st.x("UPDATE messages SET created=created-600 WHERE id=?", (old,))
        replied = st.add_message("r", "sam", True, [], "got a reply", None, None, False)
        st.add_message("r", "bob", False, [], "reply", None, replied, False)
        agent = st.add_message("r", "bob", False, [], "from an agent", None, None, False)
        own = st.add_message("r", "sam", True, [], "first", None, None, False)
        st.add_message("r", "sam", False, [], "reply", None, own, False)  # someone answered it
        own_reply = st.add_message("r", "sam", True, [], "and also", None, own, False)  # a person's reply to self
    with TestClient(create_app(s, FakeLLM())) as c:
        hub = c.app.state.hub
        for _ in range(100):
            if hub.store.message(fresh)["recipients"]:
                break
            time.sleep(0.02)
        assert hub.message_json(hub.store.message(fresh))["to"] == ["alice"]
        for mid in (old, replied, agent, own_reply):
            assert hub.store.message(mid)["recipients"] == ""


def test_window_note_waits_while_the_worker_is_busy(env):
    c, hub = env
    _machine(c)
    _on(c, "alice")
    mid = post_id(hub.post("sam", "r", "@alice hello?", human=True))
    assert [r["id"] for r in hub.jobs("alice")] == [mid]
    hub.worker_report("alice", "busy")
    hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - REDELIVER_WINDOW - 1, mid))
    hub.jobs("alice")
    assert notes(hub, mid, "alice") == []  # still on it, maybe
    hub.worker_report("alice", "online")
    hub.jobs("alice")
    assert notes(hub, mid, "alice") == [f"(alice did not answer within {REDELIVER_WINDOW // 60} minutes; not answered)"]


def test_offer_ruled_out_in_bot_mode_gets_no_note_after_done(env):
    c, hub = env
    assert c.post("/api/admin/agents", json={"name": "carol"}, headers=ADM).status_code == 200
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "kickoff"}, headers=ADM)
    assert c.put("/api/admin/rooms/b/bot", json={"on": True}, headers=ADM).status_code == 200
    goal = post_id(hub.post("human", "b", "@bob plan it", human=True))
    mid = post_id(hub.post("bob", "b", "Plan drafted.\n@alice and @carol, can you price it?", reply_to=goal))
    assert hub.jobs("carol") == []  # alice holds the baton
    hub.post("alice", "b", "[done]", reply_to=mid)
    assert not hub.bot("b")
    for _ in range(3):
        hub.jobs("carol")
    hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - REDELIVER_WINDOW - 1, mid))
    hub.jobs("carol")
    assert notes(hub, mid, "carol") == []


def test_filler_auto_answer_to_an_agent_says_no_usable_answer(env, monkeypatch):
    c, hub = env
    hub.store.create_agent("helper", model="local/small")

    async def filler(*a, **kw):
        return "(empty answer)", 10
    monkeypatch.setattr(hub.llm, "chat", filler)
    mid = post_id(hub.post("alice", "r", "@helper can you check this?"))
    asyncio.run(autoreply.autoreply_once(hub))
    assert notes(hub, mid, "helper") == ["(local/small gave no usable answer; not answered)"]
    assert autoreply.FILLER_RE.match(notes(hub, mid, "helper")[0])
