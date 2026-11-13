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
@pytest.mark.parametrize("reply,expected", [
    ("Here it is:\n```python\ndef f():\n    return 1\n```\nNotes\n```py\nsecond\n```",
     "def f():\n    return 1\n"),
    ("```\nx = 1\n```", "x = 1\n"),
    ("~~~python\nx = 1\n~~~\nNotes", "x = 1\n"),
    ("````python\nx = '```'\n`````", "x = '```'\n"),
    ("```python\npartial", "partial"),
    ("```python\n```", ""),
    ("  def f():\n      return 1\n\n", "  def f():\n      return 1\n\n"),
    ("Use `inline code` here.", "Use `inline code` here."),
])
def test_code_format_protocols(direct, protocol, reply, expected):
    app, hub, headers = direct
    calls = []
    async def chat(*args, **kw):
        calls.append(args)
        return reply, 10
    hub.llm.chat = chat
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            async def ask(**kw):
                args = {"model": "m", "prompt": "hi", **kw}
                if protocol == "rest":
                    r = await c.post("/api/v1/ask", headers=headers, json=args)
                    return r.text, r.status_code == 400
                r = await tool(hub, **args)
                return r["content"][0]["text"], r["isError"]
            assert await ask(format="code") == (expected, False)
            assert await ask() == (reply, False)
            assert await ask(format="text") == (reply, False)
            out, error = await ask(format="code", room="results")
            assert not error and out.startswith("posted ")
            message = hub.store.message(post_id(out))
            assert (message["body"] or message["summary"]) == reply.strip()
            count = len(calls)
            assert await ask(format="json") == ("error: format must be 'text' or 'code'", True)
            assert len(calls) == count
            assert await hub.ask("helper", "m", "hi", raw=True, format="code") == reply
    asyncio.run(run())


@pytest.mark.parametrize("reply,expected", [
    ("Intro\n```python\nx = 1\n```\nNotes", "x = 1\n"),
    ("Intro\n```python\nx =", "x ="),
    ("plain reply", "plain reply\n"),
])
@pytest.mark.parametrize("final_max", [100, 50])
def test_code_format_preserves_notices(direct, reply, expected, final_max):
    _, hub, _ = direct
    hub.store.set_budget("helper", 100)
    cut = []
    async def chat(*args, **kw):
        cut.append(kw["cut_notice"](final_max))
        return reply + "\n\n" + cut[-1], 90
    hub.llm.chat = chat
    out = asyncio.run(hub.ask("helper", "m", "hi", max_tokens=200, format="code"))
    assert out == (expected + "\n" + cut[0] + "\n[budget 90/100 today]"
                   "\n[max_tokens lowered from 200 to 100: what is left of today's budget]")


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


@pytest.mark.parametrize("probe", [True, False])
def test_model_list_details_and_probes(direct, monkeypatch, probe):
    from bunch.llm import LLM
    app, hub, headers = direct
    deployments = [
        {"model_name": "local/coder", "litellm_params": {"api_base": "http://192.0.2.7:11434"},
         "model_info": {"max_input_tokens": 32768, "description": "Code review\n and fixes"}},
        {"model_name": "local/general", "litellm_params": {"api_base": "http://192.0.2.7:11434/v1"}},
        {"model_name": "local/down", "litellm_params": {"api_base": "http://192.0.2.8:11434"}},
        {"model_name": "paid/big", "model_info": {"max_tokens": 64000}},
        {"model_name": "unknown"},
    ]
    requests, connects = [], []
    def handler(req):
        requests.append(req.url.path)
        if req.url.path == "/model/info":
            return httpx.Response(200, json={"data": deployments[:-1]})
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": d["model_name"]} for d in deployments]})
    llm = LLM("http://gw", "k", model_probe=probe)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hub.llm = llm
    hub.store.create_machine("workstation")
    hub.store.set_machine_report("workstation", "workstation", ["192.0.2.7"], [], "answer", [])
    class Writer:
        def close(self):
            pass
    async def run():
        both = asyncio.Event()
        async def connect(host, port):
            connects.append((host, port))
            if len(connects) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), .5)  # distinct servers must be checked concurrently
            if host == "192.0.2.8":
                raise OSError("refused")
            return None, Writer()
        monkeypatch.setattr(asyncio, "open_connection", connect)
        up = ", up" if probe else ""
        down = ", DOWN: not reachable from the hub" if probe else ""
        expected = (f"models:\nlocal/coder [workstation{up}, context 32768, Code review and fixes]\n"
                    f"local/down [192.0.2.8:11434{down}]\nlocal/general [workstation{up}]\n"
                    "paid/big [paid API, context 64000]\nunknown")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/ask", headers=headers, json={"model": "?"})
            assert r.status_code == 200 and r.text == expected
        result = await tool(hub, model="?")
        assert not result["isError"] and result["content"][0]["text"] == expected
        assert sorted(connects) == ([("192.0.2.7", 11434), ("192.0.2.8", 11434)] if probe else [])
        assert requests.count("/model/info") == 2
        await llm.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("status,data", [(403, {}), (404, {}), (200, {"data": []})])
def test_model_list_metadata_unavailable(direct, monkeypatch, status, data):
    from bunch.llm import LLM
    _, hub, _ = direct
    def handler(req):
        if req.url.path == "/model/info":
            return httpx.Response(status, json=data)
        return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})
    llm = LLM("http://gw", "k")
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hub.llm = llm
    async def connect(*args):
        pytest.fail("no probe without metadata")
    monkeypatch.setattr(asyncio, "open_connection", connect)
    async def run():
        assert await hub.ask("helper", "?") == "models: a, b"
        await llm.aclose()
    asyncio.run(run())
