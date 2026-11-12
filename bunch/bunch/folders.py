"""A room's shared folder (#84): one per room, kept on the hub, either a git repository or a plain directory.

Both kinds answer the same questions (list, read, write, delete, history, size), so the routes, the viewer
and the worker don't care which one a room has. What differs is what happens when two agents change the
same thing: a git folder keeps both (the later one on a side branch), a plain folder keeps the last write.

Git folders are bare repositories. Nothing is ever checked out on the hub and no hook ever runs here.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import uuid

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

KINDS = ("git", "plain")
# What agents and people are told about each kind, word for word, so everyone hears the same thing.
ABOUT = {
    "git": "a git repository: every change is kept with who made it and can be undone; "
           "two agents changing the same file both keep their work (the later one on a side branch)",
    "plain": "a plain folder: no history and no undo; when two agents change the same file the later one replaces "
             "the earlier",
}
BRANCH = "main"
_write_lock = threading.Lock()  # one writer at a time per hub: folder writes are short and rare


class FolderError(Exception):
    """Something the caller got wrong (bad path, too big, no such file); the message is safe to show."""


def clean_path(path: str) -> str:
    """A relative path inside the folder, or FolderError. No .., no absolute paths, nothing under .git,
    no backslashes or control characters (they mean different things on different machines)."""
    path = (path or "").strip()
    parts = path.split("/")
    if (not path or len(path) > 400 or path.startswith("/") or "\\" in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts)):
        raise FolderError(f"not a usable file path: {path[:80]!r}")
    return path


def _git(repo: str, *args: str, data: bytes | None = None, env: dict | None = None, ok=(0,)) -> bytes:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_DIR": repo, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "HOME": repo, "LC_ALL": "C"}
    try:
        p = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "gc.auto=0", *args], input=data,
                           capture_output=True, env={**base, **(env or {})}, timeout=120)
    except FileNotFoundError:
        raise FolderError("git is not installed on the hub, so git folders can't be used")
    except subprocess.TimeoutExpired:
        raise FolderError("git took too long")
    if p.returncode not in ok:
        raise FolderError("git: " + (p.stderr.decode(errors="replace").strip().splitlines() or ["failed"])[-1][:200])
    return p.stdout


def _who(author: str) -> dict:
    name = "".join(c for c in (author or "someone") if c.isalnum() or c in "_.-")[:32] or "someone"
    mail = f"{name}@bunch.invalid"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": mail, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": mail}


def _dir_size(root: str) -> int:
    total = 0
    for d, _dirs, files in os.walk(root):
        for f in files:
            try:
                total += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return total


class GitFolder:
    kind = "git"

    def __init__(self, root: str):
        self.root = root  # <folders dir>/<id>.git

    def create(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        _git(self.root, "init", "--bare", "--quiet", "--initial-branch", BRANCH)
        tree = _git(self.root, "mktree", data=b"").decode().strip()  # the empty tree: main exists from the start
        first = _git(self.root, "commit-tree", tree, "-m", "start of the room folder", env=_who("bunch")).decode().strip()
        _git(self.root, "update-ref", "refs/heads/" + BRANCH, first)

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def size(self) -> int:
        return _dir_size(self.root)

    def head(self) -> str:
        return _git(self.root, "rev-parse", "refs/heads/" + BRANCH).decode().strip()

    def list(self) -> list[dict]:
        out = []
        for line in _git(self.root, "ls-tree", "-r", "-l", "-z", BRANCH).split(b"\0"):
            if not line:
                continue
            meta, _, path = line.partition(b"\t")
            mode, typ, sha, size = meta.split()
            if typ == b"blob" and mode != b"120000":  # symlinks are never followed or shown
                out.append({"path": path.decode(errors="replace"), "size": int(size), "sha": sha.decode()})
        return out

    def read(self, path: str) -> bytes:
        path = clean_path(path)
        if not any(f["path"] == path for f in self.list()):
            raise FolderError("no such file")
        return _git(self.root, "cat-file", "blob", f"{BRANCH}:{path}")

    def _commit(self, author: str, message: str, change) -> None:
        """Builds one commit on main from an index that never touches a working tree."""
        with _write_lock, tempfile.TemporaryDirectory(prefix="bunch-index-") as tmp:
            env = {"GIT_INDEX_FILE": os.path.join(tmp, "index"), **_who(author)}
            old = self.head()
            _git(self.root, "read-tree", old, env=env)
            change(env)
            tree = _git(self.root, "write-tree", env=env).decode().strip()
            new = _git(self.root, "commit-tree", tree, "-p", old, "-m", message[:200], env=env).decode().strip()
            _git(self.root, "update-ref", "refs/heads/" + BRANCH, new, old)

    def write(self, path: str, data: bytes, author: str, base: str | None = None) -> dict:
        path = clean_path(path)

        def change(env):
            blob = _git(self.root, "hash-object", "-w", "--stdin", data=data).decode().strip()
            _git(self.root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        existed = any(f["path"] == path for f in self.list())
        self._commit(author, f"{author}: {'change' if existed else 'add'} {path}", change)
        return {"path": path, "overwrote": False}

    def delete(self, path: str, author: str) -> None:
        path = clean_path(path)
        if not any(f["path"] == path for f in self.list()):
            raise FolderError("no such file")
        self._commit(author, f"{author}: remove {path}",
                     lambda env: _git(self.root, "update-index", "-z", "--index-info", env=env,  # mode 0 = remove
                                      data=b"0 " + b"0" * 40 + b"\t" + path.encode() + b"\0"))

    def history(self, limit: int = 30) -> list[dict]:
        raw = _git(self.root, "log", f"-n{int(limit)}", "--format=%h%x1f%an%x1f%at%x1f%s%x1e", BRANCH).decode(errors="replace")
        out = []
        for rec in raw.split("\x1e"):
            f = rec.strip().split("\x1f")
            if len(f) == 4:
                out.append({"id": f[0], "who": f[1], "when": float(f[2]), "what": f[3]})
        return out[:-1] if out and out[-1]["what"] == "start of the room folder" else out

    def branches(self) -> list[dict]:
        """Work that didn't fit onto main: one side branch per agent run that conflicted."""
        raw = _git(self.root, "for-each-ref", "--format=%(refname:short)%1f%(committerdate:unix)%1f%(subject)",
                   "refs/heads/agent/").decode(errors="replace")
        out = []
        for line in raw.splitlines():
            f = line.split("\x1f")
            if len(f) == 3:
                out.append({"name": f[0], "when": float(f[1] or 0), "what": f[2]})
        return out

    # -- how a worker's checkout syncs: bundles, so the hub needs no git server ----------------------------
    def bundle(self) -> bytes:
        with tempfile.TemporaryDirectory(prefix="bunch-bundle-") as tmp:
            path = os.path.join(tmp, "b")
            _git(self.root, "bundle", "create", "--quiet", path, "refs/heads/" + BRANCH)
            with open(path, "rb") as f:
                return f.read()

    def receive(self, data: bytes, agent: str, msg_id: int) -> dict:
        """Takes the commits a worker made. They go onto main when they build on it; otherwise they are kept
        on agent/<name>/<message id> and main is left alone, so nobody's work is lost."""
        ref = "refs/incoming/" + uuid.uuid4().hex
        with _write_lock, tempfile.TemporaryDirectory(prefix="bunch-bundle-") as tmp:
            path = os.path.join(tmp, "b")
            with open(path, "wb") as f:
                f.write(data)
            _git(self.root, "-c", "fetch.fsckObjects=true", "fetch", "--quiet", "--no-tags", path,
                 f"refs/heads/bunch-send:{ref}")
            try:
                old, new = self.head(), _git(self.root, "rev-parse", ref).decode().strip()
                files = len([x for x in _git(self.root, "diff", "--name-only", "-z", f"{old}...{new}").split(b"\0") if x])
                fits = subprocess.run(["git", "--git-dir", self.root, "merge-base", "--is-ancestor", old, new],
                                      capture_output=True).returncode == 0
                if fits:
                    _git(self.root, "update-ref", "refs/heads/" + BRANCH, new, old)
                    return {"result": "merged", "files": files}
                safe = "".join(c for c in agent if c.isalnum() or c in "_.-") or "agent"
                branch = f"agent/{safe}/{int(msg_id)}"
                _git(self.root, "update-ref", "refs/heads/" + branch, new)
                return {"result": "branch", "branch": branch, "files": files}
            finally:
                _git(self.root, "update-ref", "-d", ref, ok=(0, 1))


class PlainFolder:
    kind = "plain"

    def __init__(self, root: str):
        self.root = root  # <folders dir>/<id>

    def create(self) -> None:
        os.makedirs(self.root, exist_ok=True)

    def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def size(self) -> int:
        return _dir_size(self.root)

    def _full(self, path: str) -> str:
        """The file's place on disk; refuses anything that would land outside the folder, symlinks included."""
        full = os.path.join(self.root, *clean_path(path).split("/"))
        root = os.path.realpath(self.root)
        if not os.path.realpath(os.path.dirname(full)).startswith(root) or os.path.islink(full):
            raise FolderError("not a usable file path")
        return full

    def list(self) -> list[dict]:
        out = []
        for d, dirs, files in os.walk(self.root):
            dirs[:] = sorted(x for x in dirs if not os.path.islink(os.path.join(d, x)))
            for f in sorted(files):
                full = os.path.join(d, f)
                if os.path.islink(full):
                    continue
                with open(full, "rb") as fh:
                    sha = hashlib.sha256(fh.read()).hexdigest()
                out.append({"path": os.path.relpath(full, self.root).replace(os.sep, "/"),
                            "size": os.path.getsize(full), "sha": sha})
        return out

    def read(self, path: str) -> bytes:
        full = self._full(path)
        if not os.path.isfile(full):
            raise FolderError("no such file")
        with open(full, "rb") as f:
            return f.read()

    def write(self, path: str, data: bytes, author: str, base: str | None = None) -> dict:
        """base = the version the writer started from (sha256, "" for a new file). When the file has changed
        since, the write still wins (that's what plain means) and the answer says a newer copy was replaced."""
        full = self._full(path)
        with _write_lock:
            now = None
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    now = hashlib.sha256(f.read()).hexdigest()
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = full + ".bunch-tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, full)
        return {"path": clean_path(path), "overwrote": base is not None and now is not None and now != base}

    def delete(self, path: str, author: str) -> None:
        full = self._full(path)
        if not os.path.isfile(full):
            raise FolderError("no such file")
        os.remove(full)
        d = os.path.dirname(full)
        while d != self.root and not os.listdir(d):  # don't leave empty directories behind
            os.rmdir(d)
            d = os.path.dirname(d)

    def history(self, limit: int = 30) -> list[dict]:
        return []

    def branches(self) -> list[dict]:
        return []


def open_folder(store, row):
    """The storage for a folders row."""
    base = os.path.join(store.folders_dir, str(int(row["id"])))
    return GitFolder(base + ".git") if row["kind"] == "git" else PlainFolder(base)


class FolderIn(BaseModel):
    kind: str = "git"


def add_routes(app: FastAPI, *, store, hub, s, viewer, require_admin, machine_of, machine_agent) -> None:
    """Routes for people (/api/admin/rooms/{room}/folder…) and for workers (/api/v1/machine/folder…)."""
    run = asyncio.to_thread  # git and disk work stays off the event loop

    def room_of(room: str) -> str:
        room = room.strip().lower().lstrip("#")
        if not store.room(room):
            raise HTTPException(404, "no such room")
        return room

    def folder_of(room: str):
        row = store.folder(room)
        if not row:
            raise HTTPException(404, "this room has no folder")
        return row, open_folder(store, row)

    async def guarded(fn, *args):
        try:
            return await run(fn, *args)
        except FolderError as e:
            raise HTTPException(400, str(e))

    async def room_left(f, extra: int) -> None:
        if s.max_folder and await run(f.size) + extra > s.max_folder:
            raise HTTPException(413, "the room folder is full; remove some files first")

    def changed(room: str) -> None:
        hub.broadcast({"type": "folder", "room": room})

    def describe(row, f) -> dict:
        return {"kind": row["kind"], "about": ABOUT[row["kind"]], "files": f.list(), "history": f.history(),
                "branches": f.branches(), "size": f.size(), "max": s.max_folder, "max_file": s.max_file,
                "created": row["created"], "creator": row["creator"]}

    # -- people ------------------------------------------------------------------------------------------
    @app.get("/api/admin/rooms/{room}/folder")
    async def room_folder(request: Request, room: str, authorization: str | None = Header(None)):
        viewer(request, authorization)
        row = store.folder(room_of(room))
        if not row:
            return {"folder": None, "kinds": ABOUT}
        return {"folder": await guarded(describe, row, open_folder(store, row)), "kinds": ABOUT}

    @app.post("/api/admin/rooms/{room}/folder")
    async def room_folder_add(request: Request, room: str, body: FolderIn, authorization: str | None = Header(None)):
        who = require_admin(request, authorization)
        room = room_of(room)
        if body.kind not in KINDS:
            raise HTTPException(400, "kind must be git or plain")
        if store.folder(room):
            raise HTTPException(409, "this room already has a folder")
        row = store.add_folder(room, body.kind, who)
        try:
            await run(open_folder(store, row).create)
        except FolderError as e:
            store.delete_folder(room)
            raise HTTPException(400, str(e))
        changed(room)
        hub.broadcast({"type": "rooms"})
        return {"room": room, "kind": body.kind}

    @app.delete("/api/admin/rooms/{room}/folder")
    async def room_folder_remove(request: Request, room: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        room = room_of(room)
        row, f = folder_of(room)
        store.delete_folder(room)
        await run(f.destroy)
        changed(room)
        hub.broadcast({"type": "rooms"})
        return {"deleted": room}

    @app.get("/api/admin/rooms/{room}/folder/file")
    async def room_file(request: Request, room: str, path: str, authorization: str | None = Header(None)):
        viewer(request, authorization)
        _row, f = folder_of(room_of(room))
        data = await guarded(f.read, path)
        name = path.rsplit("/", 1)[-1].encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
        # Always a download or a fetch by the page, never a document that runs here.
        return Response(data, media_type="application/octet-stream", headers={
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox",
            "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.put("/api/admin/rooms/{room}/folder/file")
    async def room_file_put(request: Request, room: str, path: str, authorization: str | None = Header(None)):
        who, _admin = viewer(request, authorization)
        room = room_of(room)
        _row, f = folder_of(room)
        data = await request.body()
        if len(data) > s.max_file:
            raise HTTPException(413, f"a file can be at most {s.max_file} bytes")
        await room_left(f, len(data))
        out = await guarded(f.write, path, data, who)
        changed(room)
        return out

    @app.delete("/api/admin/rooms/{room}/folder/file")
    async def room_file_delete(request: Request, room: str, path: str, authorization: str | None = Header(None)):
        who = require_admin(request, authorization)  # a plain folder has no undo
        room = room_of(room)
        _row, f = folder_of(room)
        await guarded(f.delete, path, who)
        changed(room)
        return {"deleted": path}

    # -- workers -----------------------------------------------------------------------------------------
    def for_agent(authorization, agent: str, room: str, write: bool = False):
        """The room's folder for one of this machine's agents. The hub decides here, not the worker:
        'answer' agents get nothing, only 'full' may change files."""
        a = machine_agent(machine_of(authorization), agent)
        power = hub.worker_config(a).get("power") or "answer"
        if power == "answer" or (write and power != "full"):
            raise HTTPException(403, f"{agent} isn't allowed to {'change' if write else 'read'} files")
        row, f = folder_of(room_of(room))
        return a["name"], row, f

    @app.get("/api/v1/machine/folder")
    async def machine_folder(agent: str, room: str, authorization: str | None = Header(None)):
        _name, row, f = for_agent(authorization, agent, room)
        return {"kind": row["kind"], "about": ABOUT[row["kind"]],
                "files": await guarded(f.list) if row["kind"] == "plain" else []}

    @app.get("/api/v1/machine/folder/bundle")
    async def machine_bundle(agent: str, room: str, authorization: str | None = Header(None)):
        _name, row, f = for_agent(authorization, agent, room)
        if row["kind"] != "git":
            raise HTTPException(400, "not a git folder")
        return Response(await guarded(f.bundle), media_type="application/octet-stream")

    @app.post("/api/v1/machine/folder/bundle")
    async def machine_bundle_in(request: Request, agent: str, room: str, id: int = 0,
                                authorization: str | None = Header(None)):
        name, row, f = for_agent(authorization, agent, room, write=True)
        if row["kind"] != "git":
            raise HTTPException(400, "not a git folder")
        data = await request.body()
        await room_left(f, len(data))
        out = await guarded(f.receive, data, name, id)
        changed(room_of(room))
        return out

    @app.get("/api/v1/machine/folder/file")
    async def machine_file_get(agent: str, room: str, path: str, authorization: str | None = Header(None)):
        _name, _row, f = for_agent(authorization, agent, room)
        return Response(await guarded(f.read, path), media_type="application/octet-stream")

    @app.put("/api/v1/machine/folder/file")
    async def machine_file_put(request: Request, agent: str, room: str, path: str, base: str | None = None,
                               authorization: str | None = Header(None)):
        name, _row, f = for_agent(authorization, agent, room, write=True)
        data = await request.body()
        if len(data) > s.max_file:
            raise HTTPException(413, f"a file can be at most {s.max_file} bytes")
        await room_left(f, len(data))
        out = await guarded(f.write, path, data, name, base)
        changed(room_of(room))
        return out

    @app.delete("/api/v1/machine/folder/file")
    async def machine_file_delete(agent: str, room: str, path: str, authorization: str | None = Header(None)):
        name, _row, f = for_agent(authorization, agent, room, write=True)
        await guarded(f.delete, path, name)
        changed(room_of(room))
        return {"deleted": path}
