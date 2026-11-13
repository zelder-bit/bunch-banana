"""Reliability fixes: worker heartbeat (#62), independent auto agents and honest notes (#64), router timeout (#76)."""
import asyncio
import dataclasses
import time

import pytest

from bunch import autoreply
from bunch.autoreply import autoreply_once
from bunch.router import route
from bunch.hub import MAX_OFFERS, post_id

from .test_bunch import _load_worker, _machine, _on, adm, env, h  # noqa: F401  (env is a fixture)


def _auto(c, *names):
    for n in names:
        assert c.post("/api/admin/agents", json={"name": n, "model": "local/small"}, headers=adm()).status_code == 200


def _from(c, room, sender):
    return [m for m in c.get(f"/api/admin/rooms/{room}/messages", headers=adm()).json() if m["sender"] == sender]


# ------------------------------------------------------------------ #62 heartbeat
def test_heartbeat_pings_while_running_and_stops_after():
    w = _load_worker()
    calls = []
    with w.heartbeat(0.02, lambda: calls.append(1)):
        time.sleep(0.3)
    n = len(calls)
    assert n >= 3
    time.sleep(0.15)
    assert len(calls) == n


def test_heartbeat_survives_failing_pings_and_stops_on_exceptions():
    w = _load_worker()
    calls = []

    def down():
        calls.append(1)
        raise OSError("hub unreachable")
    with pytest.raises(RuntimeError):
        with w.heartbeat(0.02, down) as hb:
            time.sleep(0.2)
            assert hb.thread.is_alive()
            raise RuntimeError("the run blew up")
    assert len(calls) >= 3  # kept trying after each failure
    assert not hb.thread.is_alive()  # and stopped on the exception path


def test_worker_pings_during_a_cli_run(env, monkeypatch):
    c, k, app, llm = env
    w = _load_worker()
    pings = []
    monkeypatch.setattr(w, "ping", lambda: pings.append(1))
    monkeypatch.setattr(w.heartbeat.__init__, "__defaults__", (0.02, w.ping))  # fast beat, same fake ping
    monkeypatch.setattr(w, "report", lambda *a, **kw: "")
    monkeypatch.setattr(w, "parse_output", lambda *a: ("hi", 1, 0, None))
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **kw: (time.sleep(0.3), type("P", (), {
        "stdout": "", "stderr": "", "returncode": 0})())[1])
    wk = w.Worker()
    wk.agents = {"bob": {"cli": "claude", "power": "answer"}}
    wk.reply = lambda job, text: None
    wk.handle({"agent": "bob", "id": 1, "room": "r", "sender": "human", "human": True, "text": "hi",
               "created": time.time()})
    n = len(pings)
    assert n >= 3
    time.sleep(0.1)
    assert len(pings) == n


def test_ping_route_refreshes_last_seen_and_rejects_bad_keys(env):
    c, k, app, llm = env
    key = _machine(c)
    store = app.state.hub.store
    store.x("UPDATE machines SET last_seen=? WHERE name='workstation'", (time.time() - 600,))
    assert not app.state.hub.machine_online(store.machine("workstation"))
    r = c.get("/api/v1/machine/ping", headers=h(key))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert app.state.hub.machine_online(store.machine("workstation"))
    assert c.get("/api/v1/machine/ping", headers=h("bunchm_nope")).status_code == 401
    assert c.get("/api/v1/machine/ping").status_code == 401


# ------------------------------------------------------------------ #64 auto agents
def test_a_slow_agent_does_not_hold_up_another(env):
    c, k, app, llm = env
    hub = app.state.hub
    _auto(c, "aa", "bb")
    gate = None

    async def chat(model, messages, max_tokens, user=None, **kw):
        if user == "aa":
            await gate.wait()
        return f"{user} says hi", 10
    llm.chat = chat

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        hub.post("human", "r1", "@aa slow one", human=True)
        await autoreply_once(hub, wait=False)
        hub.post("human", "r2", "@bb quick one", human=True)  # other room: the room lock isn't shared
        await autoreply_once(hub, wait=False)
        for _ in range(100):
            if _from(c, "r2", "bb"):
                break
            await asyncio.sleep(0.01)
        assert _from(c, "r2", "bb") and not _from(c, "r1", "aa")  # bb answered while aa is still busy
        gate.set()
        await autoreply_once(hub)  # waits for the rest
        assert _from(c, "r1", "aa")
    asyncio.run(scenario())


def test_queued_message_is_answered_even_if_it_gets_old_while_waiting(env, monkeypatch):
    c, k, app, llm = env
    hub = app.state.hub
    monkeypatch.setattr(autoreply, "STALE_AFTER", 1)
    _auto(c, "aa")
    gate, calls = None, []

    async def chat(model, messages, max_tokens, user=None, **kw):
        calls.append(1)
        if len(calls) == 1:
            await gate.wait()
        return "done", 10
    llm.chat = chat

    async def scenario():
        nonlocal gate
        gate = asyncio.Event()
        hub.post("human", "r", "@aa first", human=True)
        await autoreply_once(hub, wait=False)
        hub.post("human", "r", "@aa second, different text", human=True)
        await autoreply_once(hub, wait=False)  # read now, while fresh; queued behind the slow first answer
        await asyncio.sleep(1.3)  # now older than STALE_AFTER
        gate.set()
        await autoreply_once(hub)
    asyncio.run(scenario())
    assert len(_from(c, "r", "aa")) == 2 and all(m["summary"] == "done" for m in _from(c, "r", "aa"))


def test_an_unexpected_error_gives_one_note(env):
    c, k, app, llm = env
    hub = app.state.hub
    _auto(c, "helper")

    async def boom(*a, **kw):
        raise RuntimeError("kaboom")
    llm.chat = boom
    hub.post("human", "r", "@helper hi", human=True)
    asyncio.run(autoreply_once(hub))
    asyncio.run(autoreply_once(hub))
    notes = _from(c, "r", "helper")
    assert len(notes) == 1 and "helper didn't answer: unexpected error" in notes[0]["summary"]
    assert "ask again" in notes[0]["summary"]
    # and the agent still works afterwards
    async def ok(*a, **kw):
        return "back", 5
    llm.chat = ok
    hub.post("human", "r", "@helper again?", human=True)
    asyncio.run(autoreply_once(hub))
    assert _from(c, "r", "helper")[-1]["summary"] == "back"


def test_loop_cancel_stops_running_answers(env):
    c, k, app, llm = env
    hub = app.state.hub
    _auto(c, "aa")
    cancelled = []

    async def chat(model, messages, max_tokens, user=None, **kw):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise
    llm.chat = chat

    async def scenario():
        hub._event = asyncio.Event()
        hub.post("human", "r", "@aa hi", human=True)
        t = asyncio.create_task(autoreply.autoreply_loop(hub))
        await asyncio.sleep(0.3)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        assert not autoreply._state(hub)["tasks"]
    asyncio.run(scenario())
    assert cancelled


def _old(hub, mid, age):
    hub.store.x("UPDATE messages SET created=? WHERE id=?", (time.time() - age, mid))


def test_stale_human_message_to_a_working_agent_gets_a_note(env):
    c, k, app, llm = env
    hub = app.state.hub
    _auto(c, "helper")
    hub.post("human", "r", "@helper warm up", human=True)
    asyncio.run(autoreply_once(hub))  # it now has a cursor in r and has answered before
    hub.store.x("UPDATE agents SET last_seen=? WHERE name='helper'", (time.time() - 5000,))
    from bunch.hub import post_id
    mid = post_id(hub.post("human", "r", "@helper are you there?", human=True))
    _old(hub, mid, 2000)  # the hub was down when it was sent
    asyncio.run(autoreply_once(hub))
    asyncio.run(autoreply_once(hub))
    last = _from(c, "r", "helper")[-1]
    assert last["summary"].startswith("(helper didn't answer:") and "ask again" in last["summary"]
    assert len(_from(c, "r", "helper")) == 2  # warm-up answer + exactly one note


def test_backlog_for_a_fresh_auto_agent_gets_no_note(env):
    c, k, app, llm = env
    hub = app.state.hub
    from bunch.hub import post_id
    mid = post_id(hub.post("human", "old", "@newbie what's up?", human=True))  # agent doesn't exist yet
    _old(hub, mid, 2000)
    _auto(c, "newbie")
    asyncio.run(autoreply_once(hub))
    assert _from(c, "old", "newbie") == []


# ------------------------------------------------------------------ #76 router timeout
def test_router_returns_within_the_timeout_and_the_message_goes_room_wide(env):
    c, k, app, llm = env
    hub = app.state.hub
    hub.s = dataclasses.replace(hub.s, router_model="local/small", router_timeout=0.2)

    async def hang(*a, **kw):
        await asyncio.sleep(30)
    llm.chat = hang
    t = time.monotonic()
    assert asyncio.run(route(hub, "r", "what is the best camera for product photos?", sender="savannah")) == []
    assert time.monotonic() - t < 2
    t = time.monotonic()
    r = c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "anyone know about cameras?"}, headers=adm())
    assert r.status_code == 200 and time.monotonic() - t < 2
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]["to"] == []  # room-wide


def test_router_timeout_setting_reads_the_environment(monkeypatch):
    from bunch.config import load
    assert load().router_timeout == 8.0
    monkeypatch.setenv("BUNCH_ROUTER_TIMEOUT", "3.5")
    assert load().router_timeout == 3.5


def _quiet_worker(monkeypatch, said):
    w = _load_worker()
    monkeypatch.setattr(w, "report", lambda *a, **kw: "")
    wk = w.Worker()
    wk.agents = {"bob": {"cli": "claude", "power": "read", "dir": "/x"}}
    wk.reply = lambda job, text: said.append(text)
    return w, wk


def test_worker_names_a_missing_folder(env, monkeypatch, tmp_path):
    """#69: an allowed folder that isn't there was reported as "claude is not installed"."""
    said = []
    w, wk = _quiet_worker(monkeypatch, said)
    gone = str(tmp_path / "gone")
    monkeypatch.setattr(w, "effective_power", lambda cfg, *a, **kw: ("read", gone))
    wk.handle({"agent": "bob", "id": 1, "room": "r", "sender": "human", "human": True, "text": "hi",
               "created": time.time(), "age": 1})
    assert len(said) == 1 and gone in said[0] and "missing" in said[0]


def test_worker_judges_backlog_by_the_hubs_clock(env, monkeypatch):
    """#69: a worker whose clock runs ahead dropped every job without a word."""
    said = []
    w, wk = _quiet_worker(monkeypatch, said)
    job = {"agent": "bob", "id": 1, "room": "r", "sender": "human", "human": True, "text": "hi"}
    # sent ten minutes before this worker started: skipped, and the room is told
    wk.handle({**job, "created": time.time(), "age": 600 + w.STALE_AFTER})
    assert len(said) == 1 and said[0].startswith("(skipped: sent before")
    # a fresh message is not stale even if "created" (hub clock) looks an hour old to this machine
    monkeypatch.setattr(w, "effective_power", lambda cfg, *a, **kw: ("read", "/definitely/not/here"))
    wk.handle({**job, "created": time.time() - 3600, "age": 2})
    assert len(said) == 2 and "missing" in said[1]


def test_machine_read_sends_the_age(env):
    c, k, app, llm = env
    key = _machine(c)
    _on(c, "alice")
    app.state.hub.post("savannah", "r", "@alice hello", human=True)
    jobs = c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"]
    assert jobs and 0 <= jobs[0]["age"] < 60


# --------------------------------------------------------------- #63: jobs survive a worker that never answers
def _job_ids(c, key):
    return [j["id"] for j in c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"]]


def test_unanswered_job_is_offered_again_but_not_for_ever(env):
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = int(hub.post("savannah", "r", "@alice hello", human=True).split("#")[1].split()[0])
    assert _job_ids(c, key) == [mid]     # fetched; the worker dies before replying
    assert _job_ids(c, key) == [mid]     # so it is offered once more
    assert _job_ids(c, key) == []        # and then left alone: a job that kills the worker must not loop


def test_answered_job_is_not_offered_again(env):
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = int(hub.post("savannah", "r", "@alice hello", human=True).split("#")[1].split()[0])
    assert _job_ids(c, key) == [mid]
    r = c.post("/api/v1/machine/post", headers=h(key), json={"agent": "alice", "room": "r", "text": "hi", "re": mid})
    assert r.status_code == 200, r.text
    assert _job_ids(c, key) == []


def test_job_survives_a_hub_restart(env):
    """What the hub knew only in memory (who was offered what, who was waiting) is gone after a restart;
    the unanswered message is still found in the database."""
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = int(hub.post("savannah", "r", "@alice hello", human=True).split("#")[1].split()[0])
    assert _job_ids(c, key) == [mid]
    hub.offered.clear()
    hub.held.clear()
    assert _job_ids(c, key) == [mid]


def test_old_unanswered_messages_stay_quiet(env):
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = int(hub.post("savannah", "r", "@alice hello", human=True).split("#")[1].split()[0])
    assert _job_ids(c, key) == [mid]
    hub.store.x("UPDATE messages SET created=created-3600 WHERE id=?", (mid,))
    assert _job_ids(c, key) == []


def test_worker_reports_a_job_that_blows_up_and_carries_on(env, monkeypatch):
    w = _load_worker()
    wk, said, handled = w.Worker(), [], []

    def handle(job):
        handled.append(job["id"])
        if job["id"] == 1:
            raise KeyError("text")
    wk.handle, wk.hello = handle, lambda force=False: None
    wk.reply = lambda job, text: said.append((job["id"], text))
    batches = iter([{"jobs": [{"id": 1, "agent": "bob", "sender": "s", "room": "r"},
                              {"id": 2, "agent": "bob", "sender": "s", "room": "r"}]}])

    def req(*a, **kw):
        try:
            return next(batches)
        except StopIteration:
            raise SystemExit
    monkeypatch.setattr(w, "req", req)
    with pytest.raises(SystemExit):
        wk.run()
    assert handled == [1, 2]
    assert len(said) == 1 and said[0][0] == 1 and "failed on" in said[0][1] and "KeyError" in said[0][1]


# --------------------------------------------------------------- #76: show the message first, pick who answers after
def _slow_router(app, llm, go):
    hub = app.state.hub
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})
    hub.store.set_agent_info("bob", description="knows databases")

    async def pick_bob(model, messages, max_tokens, user=None, **kw):
        while not go:
            await asyncio.sleep(0.01)
        return "bob", 10
    llm.chat = pick_bob


def _last(c):
    return c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-1]


def test_unaddressed_message_appears_before_the_router_answers(env):
    c, k, app, llm = env
    hub = app.state.hub
    hub.post("alice", "r", "room exists")
    c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"]))  # bob joins the room
    c.get("/api/v1/read", params={"room": "r"}, headers=h(k["carol"]))
    go = []
    _slow_router(app, llm, go)
    r = c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "why is the db slow?"}, headers=adm())
    assert r.status_code == 200  # came back while the router is still thinking
    shown = _last(c)
    assert shown["summary"] == "why is the db slow?" and shown["to"] == []
    # agents don't read it, or anything after it in the room, until its recipients are known
    hub.post("alice", "r", "@carol unrelated")
    assert c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"])).text == "no new messages"
    assert c.get("/api/v1/read", params={"room": "r"}, headers=h(k["carol"])).text == "no new messages"
    go.append(1)
    for _ in range(200):
        if c.get(f"/api/admin/rooms/r/messages", headers=adm()).json()[-2]["to"] == ["bob"]:
            break
    assert c.get("/api/admin/rooms/r/messages", headers=adm()).json()[-2]["to"] == ["bob"]
    assert not hub.routing
    assert "why is the db slow" in c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"])).text
    assert "unrelated" in c.get("/api/v1/read", params={"room": "r"}, headers=h(k["carol"])).text


def test_a_message_nobody_is_picked_for_stays_for_the_room(env):
    c, k, app, llm = env
    hub = app.state.hub
    hub.post("savannah", "r", "room exists", human=True)  # no agent has spoken: no follow-up rule
    c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"]))
    hub.s = hub.s.__class__(**{**hub.s.__dict__, "router_model": "local/small"})

    async def nobody(model, messages, max_tokens, user=None, **kw):
        raise RuntimeError("router model is down")
    llm.chat = nobody
    c.post("/api/admin/rooms/r/messages", json={"room": "r", "text": "anyone around?"}, headers=adm())
    for _ in range(200):
        if not hub.routing:
            break
        c.get("/healthz")
    assert not hub.routing and _last(c)["to"] == []
    assert "anyone around" in c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"])).text


def test_stale_routing_mark_does_not_block_the_room(env):
    """If the router task is lost (hub code error, shutdown), reads must not wait for ever."""
    c, k, app, llm = env
    hub = app.state.hub
    hub.post("alice", "r", "room exists")
    c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"]))
    mid = int(hub.post("savannah", "r", "hello all", human=True, routing=True).split("#")[1].split()[0])
    assert c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"])).text == "no new messages"
    hub.routing[mid] = ("r", time.time() - 61)
    assert "hello all" in c.get("/api/v1/read", params={"room": "r"}, headers=h(k["bob"])).text


@pytest.mark.parametrize("reason", ["paused", "rate limit", "empty text", "text too long"])
def test_rejected_worker_reply_is_not_offered_again(env, reason):
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = post_id(hub.post("person", "r", "@alice hello", human=True))
    assert _job_ids(c, key) == [mid]
    text = "answer"
    if reason == "paused":
        hub.post("bob", "r", "another answer")
        hub.s = dataclasses.replace(hub.s, max_chain=1)
    elif reason == "rate limit":
        hub.post("alice", "elsewhere", "earlier answer")
        hub.s = dataclasses.replace(hub.s, posts_per_min=1)
    elif reason == "empty text":
        text = " "
    else:
        text = "x" * (hub.s.max_body + 1)
    for _ in range(2):  # a repeated submission must not repeat the note
        r = c.post("/api/v1/machine/post", headers=h(key),
                   json={"agent": "alice", "room": "r", "text": text, "re": mid})
        assert r.status_code == 400 and reason in r.text
        assert hub.offered[("alice", mid)] == MAX_OFFERS
        assert _job_ids(c, key) == []
        hub.offered.clear()
        assert _job_ids(c, key) == []
    notes = _from(c, "r", "alice")
    assert len(notes) == 1
    assert notes[0]["reply_to"] == mid and not notes[0]["human"]
    assert notes[0]["summary"].startswith("(alice's answer was not posted: ")
    assert reason in notes[0]["summary"]
    if reason == "paused":
        assert hub.paused("r")


def test_rejected_unsolicited_worker_post_has_no_note(env):
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = post_id(hub.post("person", "r", "hello", human=True))
    for reply_to in (None, mid, mid + 100):
        r = c.post("/api/v1/machine/post", headers=h(key),
                   json={"agent": "alice", "room": "r", "text": "", "re": reply_to})
        assert r.status_code == 400
    assert not _from(c, "r", "alice")
    assert not hub.offered


def test_failed_rejection_note_still_stops_offers(env, monkeypatch):
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "alice")
    mid = post_id(hub.post("person", "r", "@alice hello", human=True))
    assert _job_ids(c, key) == [mid]

    def fail(*args, **kwargs):
        raise RuntimeError("note failed")
    monkeypatch.setattr(hub.store, "add_message", fail)
    r = c.post("/api/v1/machine/post", headers=h(key),
               json={"agent": "alice", "room": "r", "text": "", "re": mid})
    assert r.status_code == 400 and "empty text" in r.text
    assert hub.offered[("alice", mid)] == MAX_OFFERS
    assert _job_ids(c, key) == []
