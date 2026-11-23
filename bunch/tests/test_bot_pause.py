from bunch.hub import BOT_PAUSED

from .test_bunch import _bot_room, _machine, _on, _say, adm, env, h  # noqa: F401  (env is a fixture)


def _dave(c):
    assert c.post("/api/admin/agents", json={"name": "dave"}, headers=adm()).status_code == 200


def test_a_human_post_pauses_and_addressing_an_agent_resumes(env):  # noqa: F811
    c, k, app, llm = env
    hub = app.state.hub
    _dave(c)
    _bot_room(c)
    _say(hub, "human", "@bob plan a sticker launch")
    handoff = _say(hub, "bob", "Budget is drafted.\n@carol, can you price the printer?")
    assert hub.wakes(handoff, "carol")
    stop = _say(hub, "human", "stop")
    assert not hub.wakes(handoff, "carol") and hub.bot("b")  # paused, not ended
    note = hub.store.q("SELECT * FROM messages WHERE room='b' AND reply_to=?", (stop["id"],))
    assert [(n["sender"], n["summary"]) for n in note] == [("bunch", BOT_PAUSED)]
    assert hub.message_json(stop)["to"] == []  # not routed to an agent

    go = _say(hub, "human", "@dave go")
    assert hub.message_json(go)["to"] == ["dave"]
    assert not hub.store.q("SELECT 1 FROM messages WHERE reply_to=?", (go["id"],))  # addressed: no note
    m = hub.store.message(int(hub.post("dave", "b", "Printer picked.\n@carol, can you check listings?",
                                       reply_to=go["id"]).split("#")[1]))
    assert hub.wakes(m, "carol")

    late = hub.store.message(int(hub.post("carol", "b", "Printer is $300.\n@bob next",
                                          reply_to=handoff["id"]).split("#")[1]))
    assert not hub.wakes(late, "bob") and hub.bot("b")  # stored, but hands to nobody


def test_any_signed_in_person_can_switch_bot_mode_off(env):  # noqa: F811
    c, k, app, llm = env
    _bot_room(c)
    app.state.hub.store.create_user("pat", "password one")
    c.post("/api/auth/login", json={"name": "pat", "password": "password one"})
    assert c.put("/api/admin/rooms/b/bot", json={"on": True}).status_code == 403  # on: still admins only
    r = c.put("/api/admin/rooms/b/bot", json={"on": False})
    assert r.status_code == 200 and not app.state.hub.bot("b")


def test_workers_skip_a_voided_handoff(env):  # noqa: F811
    c, k, app, llm = env
    hub = app.state.hub
    key = _machine(c)
    _on(c, "carol")
    _bot_room(c)
    _say(hub, "human", "@bob plan a sticker launch")
    handoff = _say(hub, "bob", "Budget is drafted.\n@carol, can you price the printer?")
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "stop"}, headers=adm())  # the web door
    assert not hub.routing  # not handed to the router
    jobs = c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"]
    assert [j for j in jobs if j["agent"] == "carol" and j["id"] == handoff["id"]] == []


def test_an_unaddressed_post_is_routed_when_no_run_is_live(env):  # noqa: F811
    c, k, app, llm = env
    hub = app.state.hub
    _bot_room(c)
    notes = "SELECT 1 FROM messages WHERE room='b' AND sender='bunch'"
    goal = _say(hub, "human", "Goal: a haiku about rain.")  # the first goal: nobody holds a baton
    assert not hub.store.q(notes) and not hub.run_live("b")
    seen = []
    hub.finish_routing = lambda mid, to: seen.append(mid)  # route_later hands its result here
    r = c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "Make it about snow."}, headers=adm())
    assert r.status_code == 200 and not hub.store.q(notes)
    assert seen == [goal["id"] + 1]  # routed as before
    _say(hub, "bob", "Snow haiku drafted.\n@carol, can you polish it?")
    assert hub.run_live("b")
    _say(hub, "human", "wait")
    assert len(hub.store.q(notes)) == 1  # a live run: paused, with one note


def test_an_answered_handoff_is_not_a_live_run(env):  # noqa: F811
    c, k, app, llm = env
    hub = app.state.hub
    _bot_room(c)
    _say(hub, "human", "@bob goal one")
    handoff = _say(hub, "bob", "Plan drafted.\n@carol, can you price the printer?")
    hub.post("carol", "b", "Printer is $300.", reply_to=handoff["id"])  # answered, no new handoff
    assert not hub.run_live("b")
    seen = []
    hub.finish_routing = lambda mid, to: seen.append(mid)
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "now do Y"}, headers=adm())
    assert len(seen) == 1 and not hub.store.q("SELECT 1 FROM messages WHERE room='b' AND sender='bunch'")


def test_replies_during_a_live_run(env):  # noqa: F811
    c, k, app, llm = env
    hub = app.state.hub
    _bot_room(c)
    notes = "SELECT * FROM messages WHERE room='b' AND sender='bunch' ORDER BY id"
    _say(hub, "human", "@bob goal one")
    handoff = _say(hub, "bob", "Plan drafted.\n@carol, can you price the printer?")
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "stop"}, headers=adm())
    note = hub.store.q(notes)[-1]
    _say(hub, "carol", "Printer is $300.\n@alice, can you check listings?")  # a fresh handoff: live again
    assert hub.run_live("b")
    c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "ok", "re": note["id"]}, headers=adm())
    assert not hub.routing and len(hub.store.q(notes)) == 2  # replying to the note addresses nobody: paused

    m = _say(hub, "carol", "Listings checked.\n@bob, can you write the copy?")
    r = c.post("/api/admin/rooms/b/messages", json={"room": "b", "text": "yes go on", "re": m["id"]}, headers=adm())
    reply = hub.store.message(int(r.json()["result"].split("#")[1]))
    assert hub.message_json(reply)["to"] == ["carol"] and len(hub.store.q(notes)) == 2  # resumes with carol, no note
    nxt = hub.store.message(int(hub.post("carol", "b", "Copy outline done.\n@bob, can you polish it?",
                                         reply_to=reply["id"]).split("#")[1]))
    assert hub.wakes(nxt, "bob")
    assert not hub.wakes(handoff, "carol")


def test_no_stall_note_for_a_voided_late_reply(env):  # noqa: F811
    c, k, app, llm = env
    hub = app.state.hub
    _bot_room(c)
    _say(hub, "human", "@bob goal one")
    text = "Sticker plan: Cricut Maker 3, EcoTank printer, vinyl sheets.\n@{}, can you price it?"
    handoff = _say(hub, "bob", text.format("carol"))
    _say(hub, "human", "stop")
    late = hub.store.message(int(hub.post("carol", "b", text.format("bob"), reply_to=handoff["id"]).split("#")[1]))
    assert hub.stall(late)  # it would be a loop note if it weren't voided
    assert not hub.store.q("SELECT 1 FROM messages WHERE sender='bunch' AND reply_to=?", (late["id"],))
