"""Slow-model feedback (#134) and double sends (#140)."""
import asyncio

from bunch import hub as hubmod
from bunch.app import create_app
from bunch.autoreply import _answer
from bunch.config import Settings, load
from bunch.hub import post_id
from .test_ask import FakeLLM


def make(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "hub.db"), admin_token="t", daily_tokens=0), FakeLLM())
    return app, app.state.hub


def test_ask_timeout_loaded_and_used(monkeypatch, tmp_path):
    assert load().ask_timeout == 180.0
    monkeypatch.setenv("BUNCH_ASK_TIMEOUT", "420")
    s = load(db_path=str(tmp_path / "a.db"), admin_token="t", litellm_url="http://x")
    assert s.ask_timeout == 420.0
    app = create_app(s)
    assert app.state.hub.llm.timeout == 420.0
    app.state.hub.store.db.close()


def test_typing_event_carries_model(tmp_path):
    app, hub = make(tmp_path)
    hub.store.create_agent("helper")
    seen = []
    hub.broadcast = seen.append

    async def chat(*a, **kw):
        return "hi there", 5
    hub.llm.chat = chat
    mid = post_id(hub.post("person", "chat", "@helper hello", human=True))
    agent = dict(hub.store.agent("helper"), model="local/big")
    asyncio.run(_answer(hub, agent, hub.store.message(mid), "hello"))
    typing = [e for e in seen if e["type"] == "typing"]
    assert typing[0]["model"] == "local/big" and not typing[0].get("done")
    assert typing[-1]["done"] is True


def count(hub, room="chat"):
    return hub.store.one("SELECT COUNT(*) n FROM messages WHERE room=?", (room,))["n"]


def test_double_send_stored_once(tmp_path):
    app, hub = make(tmp_path)
    first = hub.post("person", "chat", "hello  there", human=True)
    assert hub.post("person", "chat", "hello there ", human=True) == first
    assert count(hub) == 1
    assert hub.post("person", "chat", "something else", human=True) != first
    assert hub.post("other", "chat", "something else", human=True).startswith("ok #")
    assert count(hub) == 3


def test_same_text_after_window_is_stored(tmp_path, monkeypatch):
    app, hub = make(tmp_path)
    first = hub.post("person", "chat", "hello", human=True)
    real = hubmod.time.time
    monkeypatch.setattr(hubmod.time, "time", lambda: real() + hubmod.DOUBLE_SEND_WINDOW + 1)
    assert hub.post("person", "chat", "hello", human=True) != first
    assert count(hub) == 2


def test_double_send_other_sender_and_reply_to(tmp_path):
    app, hub = make(tmp_path)
    a = hub.post("person", "chat", "hello", human=True)
    assert hub.post("pat", "chat", "hello", human=True) != a
    pid = post_id(a)
    r1 = hub.post("person", "chat", "hello", human=True, reply_to=pid)
    assert r1 != a
    assert hub.post("person", "chat", "hello", human=True, reply_to=pid) == r1
