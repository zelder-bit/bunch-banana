"""Room folders (#84): one shared folder per room, git or plain, for people and for machine agents."""
import json
import shutil
import urllib.error

import pytest

from bunch.folders import FolderError, GitFolder, PlainFolder, clean_path

from .test_bunch import _load_worker, _machine, adm, env, h  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git folders need git")


def _room(c, room="proj"):
    assert c.post(f"/api/admin/rooms/{room}/messages", json={"room": room, "text": "hello"}, headers=adm()).status_code == 200
    return room


def _agent(c, name, power, machine="workstation"):
    r = c.post("/api/admin/agents", json={"name": name, "worker": {
        "mode": "worker", "machine": machine, "cli": "claude", "power": power, "dir": "/srv/app"}}, headers=adm())
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x", "a/../b", "a//b", ".git/config", "a/.GIT/x", "a\\b", "a\nb", "./a"])
def test_paths_that_leave_the_folder_are_refused(bad):
    with pytest.raises(FolderError):
        clean_path(bad)


@pytest.mark.parametrize("cls,suffix", [(GitFolder, ".git"), (PlainFolder, "")])
def test_both_kinds_store_list_read_and_delete(tmp_path, cls, suffix):
    f = cls(str(tmp_path / ("1" + suffix)))
    f.create()
    assert f.list() == []
    f.write("docs/readme.md", b"hi", "ada")
    f.write("a.txt", b"one", "ada")
    assert sorted(x["path"] for x in f.list()) == ["a.txt", "docs/readme.md"]
    assert f.read("docs/readme.md") == b"hi"
    f.delete("docs/readme.md", "ada")
    assert [x["path"] for x in f.list()] == ["a.txt"]
    with pytest.raises(FolderError):
        f.read("docs/readme.md")
    with pytest.raises(FolderError):
        f.write("../escape", b"x", "ada")


def test_git_keeps_history_and_plain_does_not(tmp_path):
    g, p = GitFolder(str(tmp_path / "1.git")), PlainFolder(str(tmp_path / "2"))
    for f in (g, p):
        f.create()
        f.write("a.txt", b"one", "ada")
        f.write("a.txt", b"two", "bob")
    assert [(x["who"], x["what"]) for x in g.history()] == [("bob", "bob: change a.txt"), ("ada", "ada: add a.txt")]
    assert p.history() == []


def test_plain_says_when_a_newer_copy_was_replaced(tmp_path):
    p = PlainFolder(str(tmp_path / "2"))
    p.create()
    p.write("a.txt", b"one", "ada")
    base = p.list()[0]["sha"]
    p.write("a.txt", b"two", "bob")
    assert p.write("a.txt", b"mine", "ada", base=base)["overwrote"] is True
    assert p.write("b.txt", b"new", "ada", base="")["overwrote"] is False
    assert p.read("a.txt") == b"mine"


def test_plain_never_follows_a_symlink_out(tmp_path):
    p = PlainFolder(str(tmp_path / "2"))
    p.create()
    (tmp_path / "secret").write_text("no")
    (tmp_path / "2" / "link").symlink_to(tmp_path / "secret")
    assert p.list() == []
    with pytest.raises(FolderError):
        p.read("link")
    with pytest.raises(FolderError):
        p.write("link", b"x", "ada")
    assert (tmp_path / "secret").read_text() == "no"


def test_admin_adds_one_folder_per_room_and_people_use_it(env):
    c, _keys, app, _llm = env
    room = _room(c)
    assert c.get(f"/api/admin/rooms/{room}/folder", headers=adm()).json()["folder"] is None
    assert c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "zip"}, headers=adm()).status_code == 400
    assert c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "git"}, headers=adm()).status_code == 200
    assert c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "plain"}, headers=adm()).status_code == 409
    assert [r["folder"] for r in c.get("/api/admin/rooms", headers=adm()).json() if r["name"] == room] == ["git"]
    r = c.put(f"/api/admin/rooms/{room}/folder/file", params={"path": "notes/a.md"}, content=b"# hi", headers=adm())
    assert r.status_code == 200, r.text
    assert c.put(f"/api/admin/rooms/{room}/folder/file", params={"path": "../a"}, content=b"x", headers=adm()).status_code == 400
    f = c.get(f"/api/admin/rooms/{room}/folder", headers=adm()).json()["folder"]
    assert f["kind"] == "git" and "git repository" in f["about"]
    assert [x["path"] for x in f["files"]] == ["notes/a.md"] and len(f["history"]) == 1
    r = c.get(f"/api/admin/rooms/{room}/folder/file", params={"path": "notes/a.md"}, headers=adm())
    assert r.content == b"# hi" and "attachment" in r.headers["content-disposition"]
    assert c.get(f"/api/admin/rooms/{room}/folder").status_code == 401
    assert c.delete(f"/api/admin/rooms/{room}/folder/file", params={"path": "notes/a.md"}, headers=adm()).status_code == 200
    # Deleting the room removes the folder's data too.
    row = app.state.hub.store.folder(room)
    import os
    path = os.path.join(app.state.hub.store.folders_dir, f"{row['id']}.git")
    assert os.path.isdir(path)
    assert c.delete(f"/api/admin/rooms/{room}", headers=adm()).status_code == 200
    assert not os.path.exists(path) and app.state.hub.store.folder(room) is None


def test_folder_quota(env):
    c, _keys, app, _llm = env
    room = _room(c)
    c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "plain"}, headers=adm())
    object.__setattr__(app.state.hub.s, "max_folder", 10)
    assert c.put(f"/api/admin/rooms/{room}/folder/file", params={"path": "a"}, content=b"12345", headers=adm()).status_code == 200
    assert c.put(f"/api/admin/rooms/{room}/folder/file", params={"path": "b"}, content=b"1234567", headers=adm()).status_code == 413


def test_hub_decides_what_a_machine_agent_may_do(env):
    c, _keys, _app, _llm = env
    key, room = _machine(c), _room(c)
    for name, power in (("talker", "answer"), ("reader", "read"), ("maker", "full")):
        _agent(c, name, power)
    c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "plain"}, headers=adm())
    c.put(f"/api/admin/rooms/{room}/folder/file", params={"path": "a.txt"}, content=b"one", headers=adm())
    q = {"room": room, "path": "a.txt"}
    assert c.get("/api/v1/machine/folder/file", params={**q, "agent": "talker"}, headers=h(key)).status_code == 403
    assert c.get("/api/v1/machine/folder/file", params={**q, "agent": "reader"}, headers=h(key)).content == b"one"
    assert c.put("/api/v1/machine/folder/file", params={**q, "agent": "reader"}, content=b"x", headers=h(key)).status_code == 403
    assert c.put("/api/v1/machine/folder/file", params={**q, "agent": "maker"}, content=b"two", headers=h(key)).status_code == 200
    assert c.get("/api/v1/machine/folder/file", params={**q, "agent": "alice"}, headers=h(key)).status_code == 403
    assert c.get("/api/v1/machine/folder/file", params={**q, "agent": "maker"}).status_code == 401
    assert c.get("/api/v1/machine/folder/bundle", params={"room": room, "agent": "maker"}, headers=h(key)).status_code == 400


def test_job_says_which_kind_of_folder_the_room_has(env):
    c, _keys, _app, _llm = env
    key, room = _machine(c), _room(c)
    _agent(c, "maker", "full")
    c.post(f"/api/admin/rooms/{room}/folder", json={"kind": "plain"}, headers=adm())
    c.post(f"/api/admin/rooms/{room}/messages", json={"room": room, "text": "@maker go"}, headers=adm())
    job = c.get("/api/v1/machine/read", headers=h(key)).json()["jobs"][0]
    assert job["folder"]["kind"] == "plain" and "no history" in job["folder"]["about"]


# ------------------------------------------------------------------ the worker's copy
def _wire(w, c, key, monkeypatch):
    """Points the worker's two request helpers at the test hub."""
    def raw(method, path, params, data=None, timeout=300):
        r = c.request(method, path, params=params, content=data, headers=h(key))
        if r.status_code >= 400:
            raise urllib.error.HTTPError(path, r.status_code, r.text, None, None)
        return r.content

    def req(method, path, body=None, params=None, timeout=120):
        return json.loads(raw(method, path, params or {}, json.dumps(body).encode() if body is not None else None))
    monkeypatch.setattr(w, "raw", raw)
    monkeypatch.setattr(w, "req", req)


def _setup(env, monkeypatch, kind):
    c, _keys, _app, _llm = env
    key, room = _machine(c), _room(c)
    _agent(c, "maker", "full")
    _agent(c, "other", "full")
    c.post(f"/api/admin/rooms/{room}/folder", json={"kind": kind}, headers=adm())
    c.put(f"/api/admin/rooms/{room}/folder/file", params={"path": "a.txt"}, content=b"one\n", headers=adm())
    w = _load_worker()
    _wire(w, c, key, monkeypatch)
    return c, room, w


def _hub_file(c, room, path):
    return c.get(f"/api/admin/rooms/{room}/folder/file", params={"path": path}, headers=adm())


@pytest.mark.parametrize("kind", ["git", "plain"])
def test_worker_fetches_runs_and_sends_changes_back(env, monkeypatch, tmp_path, kind):
    c, room, w = _setup(env, monkeypatch, kind)
    f = w.RoomFolder("maker", room, kind, rooms_dir=str(tmp_path))
    d = f.fetch()
    assert open(d + "/a.txt").read() == "one\n"
    open(d + "/a.txt", "w").write("two\n")
    import os
    os.makedirs(d + "/src")
    open(d + "/src/new.py", "w").write("print(1)\n")
    os.makedirs(d + "/bunch-out")
    open(d + "/bunch-out/pic.png", "w").write("not shared")
    assert f.publish(7) == "(changed 2 files in the room folder)"
    assert _hub_file(c, room, "a.txt").content == b"two\n" and _hub_file(c, room, "src/new.py").status_code == 200
    assert _hub_file(c, room, "bunch-out/pic.png").status_code == 400
    # Nothing changed: nothing said. A removed file goes too.
    f = w.RoomFolder("maker", room, kind, rooms_dir=str(tmp_path))
    d = f.fetch()
    assert f.publish(8) == ""
    os.remove(d + "/src/new.py")
    assert f.publish(9) == "(changed 1 file in the room folder)"
    assert _hub_file(c, room, "src/new.py").status_code == 400
    if kind == "git":
        hist = c.get(f"/api/admin/rooms/{room}/folder", headers=adm()).json()["folder"]["history"]
        assert hist[0]["who"] == "maker"


def test_git_clash_goes_to_a_side_branch_and_main_is_kept(env, monkeypatch, tmp_path):
    c, room, w = _setup(env, monkeypatch, "git")
    mine = w.RoomFolder("maker", room, "git", rooms_dir=str(tmp_path / "m"))
    theirs = w.RoomFolder("other", room, "git", rooms_dir=str(tmp_path / "o"))
    open(mine.fetch() + "/a.txt", "w").write("mine\n")
    open(theirs.fetch() + "/a.txt", "w").write("theirs\n")
    assert theirs.publish(1) == "(changed 1 file in the room folder)"
    note = mine.publish(2)
    assert "clashes with newer work" in note and "agent/maker/2" in note
    assert _hub_file(c, room, "a.txt").content == b"theirs\n"
    assert [b["name"] for b in c.get(f"/api/admin/rooms/{room}/folder", headers=adm()).json()["folder"]["branches"]] == ["agent/maker/2"]
    # The next run starts from the room's latest, not from the clashing copy.
    assert open(mine.fetch() + "/a.txt").read() == "theirs\n"


def test_git_changes_to_different_files_both_land(env, monkeypatch, tmp_path):
    c, room, w = _setup(env, monkeypatch, "git")
    mine = w.RoomFolder("maker", room, "git", rooms_dir=str(tmp_path / "m"))
    theirs = w.RoomFolder("other", room, "git", rooms_dir=str(tmp_path / "o"))
    open(mine.fetch() + "/mine.txt", "w").write("m\n")
    open(theirs.fetch() + "/theirs.txt", "w").write("t\n")
    theirs.publish(1)
    assert mine.publish(2) == "(changed 1 file in the room folder)"
    assert _hub_file(c, room, "mine.txt").content == b"m\n" and _hub_file(c, room, "theirs.txt").content == b"t\n"


def test_plain_clash_last_write_wins_and_says_so(env, monkeypatch, tmp_path):
    c, room, w = _setup(env, monkeypatch, "plain")
    mine = w.RoomFolder("maker", room, "plain", rooms_dir=str(tmp_path / "m"))
    theirs = w.RoomFolder("other", room, "plain", rooms_dir=str(tmp_path / "o"))
    open(mine.fetch() + "/a.txt", "w").write("mine\n")
    open(theirs.fetch() + "/a.txt", "w").write("theirs\n")
    theirs.publish(1)
    assert mine.publish(2) == "(changed 1 file in the room folder; replaced someone's newer copy of a.txt)"
    assert _hub_file(c, room, "a.txt").content == b"mine\n"


def test_agents_are_told_which_kind_of_folder_it_is():
    w = _load_worker()
    assert "git repository" in w.folder_hint("git", "full") and "side branch" in w.folder_hint("git", "full")
    assert "NOT a git repository" in w.folder_hint("plain", "full") and "no undo" in w.folder_hint("plain", "full")
    assert "not change them" in w.folder_hint("git", "read")
    assert w.capped_power({"power": "full"}, "read") == "read" and w.capped_power({}, "full") == "answer"
