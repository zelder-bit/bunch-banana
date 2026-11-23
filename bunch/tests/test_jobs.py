"""Hub.jobs: a responder-style client gets re-offers after a crash and takes turns; an auto agent
checks again before answering a job that waited in its queue."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from bunch import autoreply
from bunch.app import create_app
from bunch.config import Settings
from bunch.hub import MAX_OFFERS, post_id
from .test_bunch import ADMIN, FakeLLM


@pytest.fixture
def env(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "hub.db"), admin_token=ADMIN), FakeLLM())
    with TestClient(app) as c:
        keys = {n: c.post("/api/admin/agents", json={"name": n},
                          headers={"Authorization": f"Bearer {ADMIN}"}).json()["key"] for n in ("alice", "bob")}
        yield c, keys, app.state.hub


def woken(c, key, mid):
    msgs = c.get("/api/v1/read", params={"format": "json"}, headers={"Authorization": f"Bearer {key}"}).json()["messages"]
    return [m for m in msgs if m["id"] == mid and m.get("wake")]


def test_unanswered_job_is_offered_again_but_not_for_ever(env):
    c, keys, hub = env
    mid = post_id(hub.post("sam", "r", "@alice hello?", human=True))
    offers = sum(len(woken(c, keys["alice"], mid)) for _ in range(MAX_OFFERS + 3))  # reads, never answers
    assert offers == MAX_OFFERS


def test_two_responders_asked_together_answer_in_turn(env):
    c, keys, hub = env
    mid = post_id(hub.post("sam", "r", "@alice @bob what do you think?", human=True))
    assert woken(c, keys["alice"], mid)       # first in the addressed order
    assert not woken(c, keys["bob"], mid)     # alice is reading jobs, so bob waits for her
    hub.post("alice", "r", "I think yes", reply_to=mid)
    assert woken(c, keys["bob"], mid)         # its turn now: offered again although bob's cursor moved on


def _slow_first(monkeypatch):
    """Holds the first auto answer until released, so more work arrives while it runs."""
    started, release, real = asyncio.Event(), asyncio.Event(), autoreply._answer

    async def slow(hub, agent, m, body):
        if not started.is_set():
            started.set()
            await release.wait()
        await real(hub, agent, m, body)
    monkeypatch.setattr(autoreply, "_answer", slow)
    return started, release


def test_double_send_during_a_slow_answer_is_answered_once(env, monkeypatch):
    c, keys, hub = env
    hub.store.create_agent("helper", model="local/small")

    async def scenario():
        started, release = _slow_first(monkeypatch)
        a = post_id(hub.post("person", "r", "@helper hello there", human=True))
        await autoreply.autoreply_once(hub, wait=False)
        await started.wait()
        hub.store.x("UPDATE messages SET created=created-5 WHERE id=?", (a,))  # past the instant double-click merge
        b = post_id(hub.post("person", "r", "@helper hello  there", human=True))
        await autoreply.autoreply_once(hub, wait=False)  # queued while the first copy has no answer yet
        release.set()
        await autoreply.autoreply_once(hub)
        return a, b
    a, b = asyncio.run(scenario())
    assert [r["reply_to"] for r in hub.store.q("SELECT reply_to FROM messages WHERE sender='helper'")] == [a]


def test_queued_bot_handoff_voided_by_a_person_is_not_answered(env, monkeypatch):
    c, keys, hub = env
    hub.store.create_agent("zed", model="local/small")
    adm = {"Authorization": f"Bearer {ADMIN}"}
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "kickoff"}, headers=adm)
    assert c.put("/api/admin/rooms/b/bot", json={"on": True}, headers=adm).status_code == 200

    async def scenario():
        started, release = _slow_first(monkeypatch)
        hub.post("human", "b", "@zed hi", human=True)
        await autoreply.autoreply_once(hub, wait=False)
        await started.wait()
        handoff = hub.store.message(post_id(hub.post("bob", "b", "Draft done.\n@zed, can you price it?")))
        assert hub.wakes(handoff, "zed")
        await autoreply.autoreply_once(hub, wait=False)  # queued behind the slow answer
        hub.post("human", "b", "stop", human=True)
        assert not hub.wakes(handoff, "zed")
        release.set()
        await autoreply.autoreply_once(hub)
        return handoff
    handoff = asyncio.run(scenario())
    assert not hub.store.q("SELECT 1 FROM messages WHERE sender='zed' AND reply_to=?", (handoff["id"],))
