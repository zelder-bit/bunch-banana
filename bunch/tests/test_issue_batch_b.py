"""Answering and routing fixes: #130 no endless regeneration, #138 router fallback, #139 wake hint, #129 thinking off."""
import asyncio
import dataclasses
import json

import httpx

from bunch import autoreply
from bunch.autoreply import autoreply_once
from bunch.hub import HubError, post_id
from bunch.llm import LLM, LLMError
from bunch.router import route

from .test_bunch import adm, env  # noqa: F401  (env is a fixture)
from .test_ux import _auto, _from


# ------------------------------------------------------------------ #130
def test_unpostable_answer_is_not_generated_again_every_pass(env):
    c, _k, app, llm = env
    hub, calls = app.state.hub, []
    _auto(c, "aa")
    autoreply._gave_up.clear()

    async def chat(model, messages, max_tokens, user=None, **kw):
        calls.append(1)
        return "an answer", 10
    llm.chat = chat
    real_post = hub.post

    def post(sender, *a, **kw):
        if sender == "aa":
            raise HubError("rate limit: too many posts this minute")  # the answer and the note both fail
        return real_post(sender, *a, **kw)
    hub.post = post
    real_post("human", "r", "@aa hello", human=True)
    for _ in range(3):
        asyncio.run(autoreply_once(hub))
    assert len(calls) == 1 and _from(c, "r", "aa") == []


def test_gave_up_only_when_nothing_was_stored(env):
    c, _k, app, llm = env
    hub = app.state.hub
    _auto(c, "aa")
    autoreply._gave_up.clear()

    async def chat(model, messages, max_tokens, user=None, **kw):
        return "fine", 10
    llm.chat = chat
    hub.post("human", "r", "@aa hello", human=True)
    asyncio.run(autoreply_once(hub))
    assert len(_from(c, "r", "aa")) == 1 and not autoreply._gave_up


def test_agent_message_that_does_not_wake_is_not_requeued(env):
    c, _k, app, llm = env
    hub, calls = app.state.hub, []
    _auto(c, "aa")
    c.post("/api/admin/agents", json={"name": "mm"}, headers=adm())
    autoreply._gave_up.clear()

    async def chat(*a, **kw):
        calls.append(1)
        return "x", 1
    llm.chat = chat
    mid = post_id(hub.post("mm", "r", "just naming aa in passing", to=["aa"]))
    asyncio.run(autoreply_once(hub))
    assert ("aa", mid) in autoreply._gave_up and not calls


# ------------------------------------------------------------------ #138
def _router_env(env, timeout=0.2):
    c, _k, app, llm = env
    hub = app.state.hub
    hub.s = dataclasses.replace(hub.s, router_model="local/small", router_timeout=timeout)
    _auto(c, "aa", "bb")

    async def down(*a, **kw):
        raise LLMError("gateway unreachable")
    llm.chat = down
    return hub


def test_router_failure_falls_back_to_the_last_answerer_of_this_sender(env):
    hub = _router_env(env)
    q = post_id(hub.post("savannah", "r", "@bb what machine for stickers?", human=True))
    hub.post("bb", "r", "a Cricut", reply_to=q)
    hub.store.x("UPDATE messages SET created=created-300 WHERE sender='bb'")
    text = "and what is the best camera for product photos okay"
    assert asyncio.run(route(hub, "r", text, sender="savannah")) == ["bb"]
    assert asyncio.run(route(hub, "r", text, sender="other")) == []


def test_router_failure_with_no_recent_answer_picks_nobody(env):
    hub = _router_env(env)
    assert asyncio.run(route(hub, "r", "what is the best camera for product photos?", sender="savannah")) == []


# ------------------------------------------------------------------ #139
def test_hint_when_an_agent_asks_a_model_agent_outside_a_persons_thread(env):
    c, _k, app, _llm = env
    hub = app.state.hub
    _auto(c, "aa")
    c.post("/api/admin/agents", json={"name": "mm"}, headers=adm())
    c.post("/api/admin/agents", json={"name": "man"}, headers=adm())
    assert "answers agents only" not in hub.post("mm", "r", "@aa, can you check this?")  # a first message still wakes it
    top = post_id(hub.post("mm", "r", "starting"))
    deep = post_id(hub.post("mm", "r", "next", reply_to=top))
    out = hub.post("mm", "r", "@aa, can you check this?", reply_to=deep)  # too deep, no person above
    assert "aa answers agents only under a person's message" in out and "bot mode" in out
    assert "answers agents only" in hub.post("mm", "r", "please look", to=["aa"], reply_to=deep)
    assert "answers agents only" not in hub.post("mm", "r", "@man, can you check this?", reply_to=deep)  # manual
    q = post_id(hub.post("human", "r", "start", human=True))
    one = post_id(hub.post("mm", "r", "x", reply_to=q))
    assert "answers agents only" not in hub.post("mm", "r", "@aa, can you check this?", reply_to=one)  # person in chain
    c.put("/api/admin/rooms/r/bot", json={"on": True}, headers=adm())
    assert "answers agents only" not in hub.post("mm", "r", "@aa, can you check this?", reply_to=deep)


# ------------------------------------------------------------------ #129
def _thinking_llm(accept):
    """Gateway whose thinking model gives no text unless the request carries a key in accept; others get 400."""
    sent = []

    def handler(req):
        if req.url.path == "/model/info":
            return httpx.Response(200, json={"data": []})
        body = json.loads(req.content)
        sent.append({k: v for k, v in body.items() if k in ("reasoning_effort", "think")})
        for k in ("reasoning_effort", "think"):
            if k in body and k not in accept:
                return httpx.Response(400, json={"error": {"message": f"invalid {k} value"}})
        text = "ok" if any(k in body for k in accept) else ""
        return httpx.Response(200, json={"choices": [{"message": {"content": text, "reasoning_content": "hm"}}],
                                         "usage": {"total_tokens": 10}})
    llm = LLM("http://gw", "k", think_max_tokens=0)
    llm._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm, sent


def test_thinking_off_falls_back_to_think_false_and_remembers_it():
    llm, sent = _thinking_llm({"think"})
    assert asyncio.run(llm.chat("m", [], 50)) == ("ok", 20)
    assert sent == [{}, {"reasoning_effort": "none"}, {"think": False}]
    sent.clear()
    assert asyncio.run(llm.chat("m", [], 50))[0] == "ok"
    assert sent == [{}, {"think": False}]  # straight to the variant that worked


def test_thinking_off_both_rejected_is_remembered():
    llm, sent = _thinking_llm(set())
    assert asyncio.run(llm.chat("m", [], 50))[0] == ""
    assert len(sent) == 3
    sent.clear()
    asyncio.run(llm.chat("m", [], 50))
    assert sent == [{}]


def test_thinking_off_reasoning_effort_still_first():
    llm, sent = _thinking_llm({"reasoning_effort"})
    assert asyncio.run(llm.chat("m", [], 50))[0] == "ok"
    assert sent == [{}, {"reasoning_effort": "none"}]


def test_thinking_off_budget_still_applies():
    llm, sent = _thinking_llm({"think"})
    asyncio.run(llm.chat("m", [], 50, remaining=5))  # first call used 10 >= 5: no last resort
    assert sent == [{}]
