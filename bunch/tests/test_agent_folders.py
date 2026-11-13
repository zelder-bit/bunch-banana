"""Agents with their own key can use a room's shared folder (#84 phase 2, resolves #106)."""
from bunch.store import text_of

from .test_bunch import adm, env, h, tool  # noqa: F401  (env is a fixture)


def _folder(c, room="proj", kind="plain"):
    assert c.post(f"/api/admin/rooms/{room}/messages", json={"room": room, "text": "hi"}, headers=adm()).status_code == 200
    assert c.post(f"/api/admin/rooms/{room}/folder", json={"kind": kind}, headers=adm()).status_code == 200
    return room


def test_agent_lists_reads_writes_and_logs(env):
    c, keys, _app, _llm = env
    room = _folder(c)
    key = keys["alice"]
    out, err = tool(c, key, "files", room=room, op="list")
    assert not err and "(empty)" in out
    assert c.get(f"/api/v1/folders/{room}", headers=h(key)).text.startswith(f"#{room}\n(empty)")
    # write via REST, read via MCP
    assert c.put(f"/api/v1/folders/{room}/file", params={"path": "notes/a.md"}, content=b"# hi\n",
                 headers=h(key)).status_code == 200
    out, err = tool(c, key, "files", room=room, op="read", path="notes/a.md")
    assert not err and out.endswith("# hi\n")
    out, _ = tool(c, key, "files", room=room, op="list")
    assert "notes/a.md" in out and "bytes" in out
    out, _ = tool(c, key, "files", room=room, op="log")  # a plain folder has no history
    assert "(no changes yet)" in out
    # write via MCP, read via REST; a note lands in the room
    out, err = tool(c, key, "files", room=room, op="write", path="b.txt", text="two")
    assert not err and out == "ok (changed b.txt)"
    assert c.get(f"/api/v1/folders/{room}/file", params={"path": "b.txt"}, headers=h(key)).text.endswith("two")
    notes = [text_of(m) for m in c.get(f"/api/admin/rooms/{room}/messages", headers=adm()).json()]
    assert any("alice changed b.txt in the room folder" in n for n in notes)
    # auth and a missing folder
    assert c.get(f"/api/v1/folders/{room}").status_code == 401
    assert c.get("/api/v1/folders/nope", headers=h(key)).status_code == 400


def test_read_refuses_binary_and_bad_paths(env):
    c, keys, _app, _llm = env
    room = _folder(c)
    key = keys["alice"]
    c.put(f"/api/v1/folders/{room}/file", params={"path": "pic.png"}, content=b"\x00\x01\xff", headers=h(key))
    out, err = tool(c, key, "files", room=room, op="read", path="pic.png")
    assert err and "not a text file" in out
    assert c.get(f"/api/v1/folders/{room}/file", params={"path": "pic.png"}, headers=h(key)).status_code == 400
    out, err = tool(c, key, "files", room=room, op="read", path="../x")
    assert err
    out, err = tool(c, key, "files", room=room, op="read", path="missing")
    assert err and "no such file" in out
    out, err = tool(c, key, "files", room=room, op="nonsense")
    assert err and "op must be" in out


def test_ask_files_appends_folder_text(env):
    c, keys, _app, llm = env
    room = _folder(c)
    key = keys["alice"]
    c.put(f"/api/v1/folders/{room}/file", params={"path": "spec.txt"}, content=b"the spec", headers=h(key))
    calls = []

    async def chat(model, msgs, *a, **kw):
        calls.append(msgs)
        return "ok", 10
    llm.chat = chat
    out, err = tool(c, key, "ask", model="m", prompt="do it", room=room, files=["spec.txt"])
    assert not err and out.startswith("posted ")
    user = calls[0][0]["content"]
    assert "Files from #proj folder:" in user and "== spec.txt ==" in user and "the spec" in user
    assert "do it" in user
    # files needs a room
    out, err = tool(c, key, "ask", model="m", prompt="x", files=["spec.txt"])
    assert err and "files needs a room" in out


def test_ask_files_over_budget_is_refused(env):
    c, keys, app, _llm = env
    room = _folder(c)
    key = keys["alice"]
    object.__setattr__(app.state.hub.s, "max_body", 10)
    c.put(f"/api/v1/folders/{room}/file", params={"path": "a"}, content=b"123456", headers=h(key))
    c.put(f"/api/v1/folders/{room}/file", params={"path": "b"}, content=b"123456", headers=h(key))
    out, err = tool(c, key, "ask", model="m", prompt="x", room=room, files=["a", "b"])
    assert err and "more than 10 characters" in out
