import asyncio
import dataclasses
import time

import httpx
import pytest

from bunch.app import create_app
from bunch.autoreply import serve_message
from bunch.config import Settings
from bunch.hub import MAX_OFFERS, Hub, post_id
from bunch.router import route
from .test_bunch import FakeLLM


@pytest.fixture
def direct(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "hub.db"), router_model="local/small", admin_token="test-admin-token"), FakeLLM())
    hub = app.state.hub
    st = hub.store
    key = st.create_machine("workstation")
    st.machine_by_key(key)
    for name in ("coder", "reviewer"):
        st.create_agent(name)
        st.set_worker(name, {"mode": "worker", "machine": "workstation", "cli": "codex"})
    yield app, hub, key
    st.db.close()


@pytest.mark.parametrize("reason", ["paused", "rate limit", "empty text", "text too long"])
def test_rejection_survives_restart_and_releases_next_agent(direct, reason):
    app, hub, key = direct
    mid = post_id(hub.post("person", "r", "@coder @reviewer hello", human=True))
    m = hub.store.message(mid)
    assert hub.turn_blocker(m, "reviewer") == "coder"
    text = "answer"
    if reason == "paused":
        hub.post("reviewer", "r", "earlier")
        hub.s = dataclasses.replace(hub.s, max_chain=1)
    elif reason == "rate limit":
        hub.post("coder", "elsewhere", "earlier")
        hub.s = dataclasses.replace(hub.s, posts_per_min=1)
    elif reason == "empty text":
        text = " "
    else:
        text = "x" * (hub.s.max_body + 1)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            for _ in range(2):
                r = await c.post("/api/v1/machine/post", headers={"Authorization": f"Bearer {key}"},
                                 json={"agent": "coder", "room": " #R ", "text": text, "re": mid})
                assert r.status_code == 400 and reason in r.text
                hub.offered.clear()
                jobs = (await c.get("/api/v1/machine/read",
                                   headers={"Authorization": f"Bearer {key}"})).json()["jobs"]
                assert not any(j["agent"] == "coder" and j["id"] == mid for j in jobs)
                assert any(j["agent"] == "reviewer" and j["id"] == mid for j in jobs)
            await serve_message(hub, hub.store.agent("coder"), m)
    asyncio.run(check())
    assert not hub.llm.calls
    notes = hub.store.q("SELECT * FROM messages WHERE reply_to=?", (mid,))
    assert len(notes) == 1
    note = notes[0]
    assert note["sender"] == "coder" and not note["human"] and not note["auto_sum"]
    assert note["recipients"] == ",person," and note["body"] is None
    restarted = Hub(hub.store, hub.s, hub.llm)
    assert not restarted.unanswered("coder")
    assert restarted.turn_blocker(m, "reviewer") is None
    assert hub.paused("r") == (reason == "paused")


@pytest.mark.parametrize("kind", ["rejected", "offline"])
def test_unanswered_notices_allow_retry_and_follow_up_without_advancing_chain(direct, kind):
    app, hub, key = direct
    hub.s = dataclasses.replace(hub.s, max_chain=1, posts_per_min=1)
    hub.post("coder", "elsewhere", "used rate allowance")
    if kind == "offline":
        hub.store.x("UPDATE machines SET last_seen=0")
    mid = post_id(hub.post("person", "r", "@coder hello", human=True))
    if kind == "rejected":
        hub.rejected_reply("coder", "r", mid, "rate limit: too many posts this minute")
    assert hub.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender='coder'", (mid,))
    assert not hub.paused("r")
    hub.store.x("UPDATE machines SET last_seen=?", (time.time(),))
    assert asyncio.run(route(hub, "r", "try again", sender="person")) == ["coder"]
    again = post_id(hub.post("person", "r", "@coder hello", human=True))
    assert hub.duplicate_of(hub.store.message(again)) is None
    hub.s = dataclasses.replace(hub.s, posts_per_min=0)
    hub.post("coder", "r", "actual answer", reply_to=again)
    assert hub.paused("r")
    hub.rejected_reply("coder", "r", mid, "paused: waiting")
    assert hub.paused("r")
    third = post_id(hub.post("person", "r", "@coder hello", human=True))
    assert hub.duplicate_of(hub.store.message(third)) == again


@pytest.mark.parametrize("room", ["elsewhere", "bad room", ""])
def test_rejected_wrong_room_has_no_note(direct, room):
    app, hub, key = direct
    mid = post_id(hub.post("person", "r", "@coder hello", human=True))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/machine/post", headers={"Authorization": f"Bearer {key}"},
                             json={"agent": "coder", "room": room, "text": "", "re": mid})
            assert r.status_code == 400
    asyncio.run(check())
    assert not hub.store.q("SELECT * FROM messages WHERE reply_to=?", (mid,))
    assert not hub.offered


def test_rejected_retry_after_answer_has_no_note(direct):
    app, hub, key = direct
    mid = post_id(hub.post("person", "r", "@coder hello", human=True))
    hub.post("coder", "r", "answer", reply_to=mid)
    hub.rejected_reply("coder", "r", mid, "empty text")
    assert len(hub.store.q("SELECT * FROM messages WHERE reply_to=?", (mid,))) == 1


def test_failed_note_preserves_rejection_and_marks_offered(direct, monkeypatch):
    app, hub, key = direct
    mid = post_id(hub.post("person", "r", "@coder hello", human=True))

    def fail(*args, **kwargs):
        raise RuntimeError("storage unavailable")
    monkeypatch.setattr(hub.store, "add_message", fail)
    hub.rejected_reply("coder", "r", mid, "empty text")
    assert hub.offered[("coder", mid)] == MAX_OFFERS


@pytest.mark.parametrize("kind", ["rejected", "offline"])
def test_notice_bypasses_chain_limit_without_unpausing(direct, kind):
    app, hub, key = direct
    mid = post_id(hub.post("person", "r", "@coder hello", human=True))
    hub.post("reviewer", "r", "answer")
    hub.s = dataclasses.replace(hub.s, max_chain=1)
    assert hub.paused("r")
    if kind == "offline":
        hub.store.x("UPDATE machines SET last_seen=0")
        hub._unavailable_notices(mid)
    else:
        hub.rejected_reply("coder", "r", mid, "paused: waiting")
    assert hub.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender='coder'", (mid,))
    assert hub.paused("r")


def test_unsolicited_rejection_has_no_note(direct):
    app, hub, key = direct
    mid = post_id(hub.post("person", "r", "hello", human=True))
    for reply_to in (None, mid, mid + 100):
        hub.rejected_reply("coder", "r", reply_to, "empty text")
    assert hub.store.room_max_id("r") == mid
    assert not hub.offered
