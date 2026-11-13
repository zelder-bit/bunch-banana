"""Auto queues survive restarts; disconnected MCP readers leave messages unread (#63)."""
import asyncio
import time

import pytest

from bunch import autoreply, mcp
from bunch.app import create_app
from bunch.config import Settings
from bunch.hub import post_id
from .test_bunch import FakeLLM


@pytest.fixture
def hub(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "hub.db"), admin_token="test-admin-token"), FakeLLM())
    hub = app.state.hub
    hub.store.create_agent("helper", model="local/small")
    yield hub
    hub.store.db.close()


def test_auto_recovers_queue_after_restart(hub):
    async def scenario():
        mid = post_id(hub.post("person", "r", "@helper hello", human=True))
        await autoreply.autoreply_once(hub, wait=False)
        assert [m["id"] for m in autoreply._state(hub)["queue"]["helper"]] == [mid]
        assert hub.store.cursor("helper", "r") >= mid
        await autoreply._stop_tasks(hub)
        del hub.__dict__["_autoreply"]
        await autoreply.autoreply_once(hub)
        await autoreply.autoreply_once(hub)
        assert len(hub.store.q("SELECT * FROM messages WHERE sender='helper' AND reply_to=?", (mid,))) == 1
    asyncio.run(scenario())


def test_auto_does_not_requeue_active_or_waiting_messages(hub, monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def answer(hub, agent, m):
            calls.append(m["id"])
            started.set()
            await release.wait()
            hub.post("helper", "r", "done", reply_to=m["id"])
        monkeypatch.setattr(autoreply, "answer", answer)
        first = post_id(hub.post("person", "r", "@helper first", human=True))
        await autoreply.autoreply_once(hub, wait=False)
        await started.wait()
        second = post_id(hub.post("person", "r", "@helper second", human=True))
        await autoreply.autoreply_once(hub, wait=False)
        await autoreply.autoreply_once(hub, wait=False)
        assert [m["id"] for m in autoreply._state(hub)["queue"]["helper"]] == [second]
        # Already queued work stays eligible even after it grows old.
        hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - 400, second))
        release.set()
        await autoreply.autoreply_once(hub)
        assert calls == [first, second]
    asyncio.run(scenario())


@pytest.mark.parametrize("skip", ["stale", "gave_up", "duplicate", "routing"])
def test_auto_recovery_respects_skips(hub, monkeypatch, skip):
    mid = post_id(hub.post("person", "r", "@helper hello", human=True))
    hub.read_rows("helper")  # cursor has passed the message, but there is no answer
    if skip == "stale":
        hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - 400, mid))
    elif skip == "gave_up":
        monkeypatch.setattr(autoreply, "_gave_up", {("helper", mid): time.time()})
    elif skip == "duplicate":
        monkeypatch.setattr(hub, "duplicate_of", lambda m: mid)
    else:
        hub.routing[mid] = ("r", time.time())
    asyncio.run(autoreply.autoreply_once(hub))
    assert not hub.store.q("SELECT * FROM messages WHERE sender='helper'")
    if skip == "routing":
        hub.finish_routing(mid, ["helper"])
        asyncio.run(autoreply.autoreply_once(hub))
        assert len(hub.store.q("SELECT * FROM messages WHERE sender='helper'")) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_mcp_disconnect_during_wait_preserves_cursor(hub, monkeypatch, batch):
    hub.post("person", "r", "warm up", human=True)
    hub.read("helper")
    before = hub.store.cursor("helper", "r")
    disconnected, mids = False, []

    async def gone():
        return disconnected

    async def wait(timeout):
        nonlocal disconnected
        mids.append(post_id(hub.post("person", "r", "@helper new work", human=True)))
        disconnected = True
    monkeypatch.setattr(hub, "wait_for_new", wait)
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "read", "arguments": {"wait": 60}}}
    asyncio.run(mcp.handle(hub, "helper", [msg] if batch else msg, gone))
    assert mids and hub.store.cursor("helper", "r") == before
    assert f"#{mids[0]}" in asyncio.run(mcp.call_tool(hub, "helper", "read", {}))


def test_deleted_agent_offers_are_dropped(hub):
    hub.offered.update({("helper", 1): 2, ("helper", 2): 1, ("other", 1): 1})
    hub.agents_changed("helper")
    assert hub.offered == {("other", 1): 1}
