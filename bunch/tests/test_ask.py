"""Ask diagnostics and protocol contracts, without a live gateway."""
import asyncio
import json

import httpx
import pytest

from bunch.app import create_app
from bunch.autoreply import FILLER_RE, _answer
from bunch.config import Settings
from bunch.hub import HubError, post_id
from bunch.mcp import handle_one
from .test_bunch import FakeLLM
from .test_llm import make


@pytest.mark.parametrize("thought,cut,expected", [
    (True, True, "used all 50 tokens on reasoning"),
    (True, False, "produced reasoning but no final text"),
    (False, True, "hit max_tokens=50 without final text"),
    (False, False, "returned nothing"),
])
def test_empty_reason(thought, cut, expected):
    reasons = []
    llm = make(lambda req: httpx.Response(200, json={
        "choices": [{"message": {"content": " \n", "reasoning_content": "hmm" if thought else ""},
                     "finish_reason": "length" if cut else "stop"}],
        "usage": {"total_tokens": 80}}))
    text, tokens = asyncio.run(llm.chat("m", [], 50, remaining=80, empty_notice=reasons.append))
    assert text == "" and tokens == 80
    assert len(reasons) == 1 and expected in reasons[0]


@pytest.mark.parametrize("fallback", ["empty", "rejected", "answer"])
def test_empty_reason_after_retries(fallback):
    sent, reasons = [], []
    def handler(req):
        payload = json.loads(req.content)
        sent.append(payload["max_tokens"])
        last = "reasoning_effort" in payload
        if last and fallback == "rejected":
            return httpx.Response(400, json={"error": "unsupported"})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK" if last and fallback == "answer" else "",
                                      "reasoning": "" if last else "hmm"},
                         "finish_reason": "stop" if last else "length"}],
            "usage": {"total_tokens": 100}})
    text, tokens = asyncio.run(make(handler).chat("m", [], 50, empty_notice=reasons.append))
    assert sent == [50, 500, 50]
    assert tokens == (200 if fallback == "rejected" else 300)
    if fallback == "answer":
        assert text == "OK" and not reasons
    else:
        assert text == ""
        assert reasons == ["the model returned nothing" if fallback == "empty" else
                           "the model used all 500 tokens on reasoning; ask with a higher max_tokens"]


@pytest.fixture
def direct(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "hub.db"), admin_token="test-admin-token",
                              daily_tokens=0), FakeLLM())
    hub = app.state.hub
    key = hub.store.create_agent("helper")
    yield app, hub, {"Authorization": f"Bearer {key}"}
    hub.store.db.close()


async def tool(hub, **args):
    return (await handle_one(hub, "helper", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "ask", "arguments": args}}))["result"]


@pytest.mark.parametrize("protocol", ["rest", "mcp"])
def test_note_and_validation(direct, protocol):
    app, hub, headers = direct
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            async def ask(**kw):
                args = {"model": "m", "prompt": "hi", **kw}
                if protocol == "rest":
                    r = await c.post("/api/v1/ask", headers=headers, json=args)
                    return r.text, r.status_code == 400
                r = await tool(hub, **args)
                return r["content"][0]["text"], r["isError"]
            for note in ("Purpose:\n  review", "x" * 200, ""):
                text, error = await ask(room="results", note=note)
                assert not error
                m = hub.store.message(post_id(text))
                assert (m["body"] or m["summary"]) == (" ".join(note.split()) + "\n" if note else "") + "answer from m"
                assert hub.llm.calls[-1][1] == [{"role": "user", "content": "hi"}]
            count = len(hub.llm.calls)
            for args in ({"room": "results", "note": "x" * 201}, {"note": "purpose"}):
                text, error = await ask(**args)
                assert error and text.startswith("error: note")
            assert len(hub.llm.calls) == count
    asyncio.run(run())


def test_empty_answer_and_auto_notice(direct):
    app, hub, _ = direct
    reason = "the model used all 50 tokens on reasoning; ask with a higher max_tokens"
    async def chat(*args, **kw):
        kw["empty_notice"](reason)
        return "", 50
    hub.llm.chat = chat
    async def run():
        expected = f"(empty answer: {reason})"
        assert await hub.ask("helper", "m", "hi") == expected
        assert await hub.ask("helper", "m", "hi", raw=True) == ""
        out = await hub.ask("helper", "m", "hi", room="results", note="Purpose")
        m = hub.store.message(post_id(out))
        assert (m["body"] or m["summary"]) == "Purpose\n" + expected
        mid = post_id(hub.post("person", "chat", "@helper hello", human=True))
        agent = dict(hub.store.agent("helper"), model="m")
        await _answer(hub, agent, hub.store.message(mid), "hello")
        reply = hub.store.one("SELECT * FROM messages WHERE reply_to=?", (mid,))
        assert (reply["body"] or reply["summary"]) == expected
        assert FILLER_RE.match(expected)
        assert hub.store.usage_today("helper")["ask"] == 200
    asyncio.run(run())


@pytest.mark.parametrize("method,path,body,target", [
    ("GET", "/api/v1/read", None, "read"),
    ("GET", "/api/v1/read?format=json", None, "read_rows"),
    ("POST", "/api/v1/post", {"room": "r", "text": "hello", "to": "helper"}, "post"),
    ("GET", "/api/v1/get/1", None, "get"),
    ("POST", "/api/v1/ask", {"model": "m", "prompt": "hi"}, "ask"),
    ("GET", "/api/v1/rooms", None, "rooms"),
    ("GET", "/api/v1/whoami", None, "agent_by_key"),
])
def test_rest_hub_error_shape(direct, monkeypatch, method, path, body, target):
    app, hub, headers = direct
    def fail(*args, **kw):
        raise HubError("failure")
    async def async_fail(*args, **kw):
        fail()
    monkeypatch.setattr(hub.store if target == "agent_by_key" else hub, target,
                        async_fail if target == "ask" else fail)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.request(method, path, headers=headers, json=body)
            assert r.status_code == 400 and r.text == "error: failure"
            assert r.headers["content-type"].startswith("text/plain")
    asyncio.run(run())


def test_model_failure_protocols(direct):
    from bunch.llm import LLMError
    app, hub, headers = direct
    async def fail(*args, **kw):
        raise LLMError("model failed")
    hub.llm.chat = fail
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/ask", headers=headers, json={"model": "m", "prompt": "hi"})
            assert r.status_code == 400 and r.text == "error: model failed"
        r = await tool(hub, model="m", prompt="hi")
        assert r == {"content": [{"type": "text", "text": "error: model failed"}], "isError": True}
    asyncio.run(run())
