"""HTTP server: MCP endpoint, agent REST API, admin API, web viewer."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from . import mcp as mcpmod
from .autoreply import autoreply_loop
from .config import Settings, load
from .hub import MENTION_RE, WORKER_CLIS, WORKER_POWERS, Hub, HubError
from .llm import LLM, LLMError
from .router import route
from .store import DEFAULT_HUMAN_AVATAR, HUMAN_AVATARS, RESERVED_NAMES, Store
from .summarizer import summarizer_loop

log = logging.getLogger("bunch")
STATIC = Path(__file__).parent / "static"


class BotIn(BaseModel):
    on: bool


class PostIn(BaseModel):
    room: str
    text: str
    summary: str | None = None
    to: str | list[str] | None = None
    re: int | None = None


class AskIn(BaseModel):
    model: str
    prompt: str | None = None
    system: str | None = None
    max_tokens: int | None = None
    room: str | None = None


class WorkerIn(BaseModel):
    mode: str = "manual"
    machine: str | None = None
    cli: str | None = None
    power: str | None = None
    model: str | None = None
    deep_model: str | None = None
    dir: str | None = None
    max_output: int | None = None
    max_input_chars: int | None = None


class AgentIn(BaseModel):
    name: str
    daily_tokens: int | None = None
    description: str = ""
    model: str | None = None
    system: str | None = None
    worker: WorkerIn | None = None


class AgentInfoIn(BaseModel):
    description: str | None = None
    model: str | None = None
    system: str | None = None


class LoginIn(BaseModel):
    name: str
    password: str


class UserIn(BaseModel):
    name: str
    password: str
    admin: bool = False


class AvatarIn(BaseModel):
    avatar: str | None = None  # None = back to the default orange


class PasswordIn(BaseModel):
    password: str
    current_password: str | None = None  # required when you change your own password from a session


class AdminFlagIn(BaseModel):
    admin: bool


class BudgetIn(BaseModel):
    daily_tokens: int | None = None


class FirstIn(BaseModel):
    on: bool = False


class ReportIn(BaseModel):
    agent: str
    status: str
    tokens: int = 0
    cached: int = 0     # cache reads, not in tokens (older workers leave it out)
    calls: int = 0
    until: float | None = None
    room: str | None = None


class MachinePostIn(BaseModel):
    agent: str
    room: str
    text: str
    re: int | None = None


class PurgeIn(BaseModel):
    ids: list[int] | None = None
    room: str | None = None
    older_than_days: float | None = None


class RetentionIn(BaseModel):
    days: float = 0   # 0 = keep forever


class HelloIn(BaseModel):
    hostname: str = ""
    addresses: list[str] = []
    clis: list[dict] = []
    max_power: str = "answer"
    dirs: list[str] = []
    ollama: dict | None = None   # {port, models}: only when the machine shares its Ollama models


class ModelIn(BaseModel):
    name: str          # what clients ask for, e.g. local/coder
    model: str         # LiteLLM's model string, e.g. ollama_chat/qwen2.5-coder:14b
    api_base: str | None = None
    check: bool = True  # send one short message through the gateway before keeping it


class ModelRefIn(BaseModel):
    name: str
    to: str | None = None
    force: bool = False


MODEL_NAME_RE = re.compile(r"[A-Za-z0-9._:/-]{1,64}")


class MachineIn(BaseModel):
    name: str


def _bearer(value: str | None) -> str:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def _worker_view(cfg: dict, admin: bool) -> dict:
    """Everyone sees where and how an agent runs; only admins see its folder and limits."""
    return cfg if admin else {k: cfg[k] for k in ("mode", "machine", "cli", "power", "model") if k in cfg}


def _machine_json(hub: Hub, m, admin: bool) -> dict:
    out = {"name": m["name"], "hostname": m["hostname"], "last_seen": m["last_seen"],
           "online": hub.machine_online(m), "clis": json.loads(m["clis"] or "[]"),
           "max_power": m["max_power"], "agents": [a["name"] for a in hub.machine_agents(m["name"])]}
    if admin:
        out["dirs"] = json.loads(m["dirs"] or "[]")
        out["addresses"] = json.loads(m["addresses"] or "[]")
        out["ollama"] = ollama_share(m)
    return out


def ollama_share(m) -> dict | None:
    """The Ollama models a machine shares, and the URL the gateway would use to reach them."""
    o = json.loads(m["ollama"] or "null")
    if not o:
        return None
    addrs = json.loads(m["addresses"] or "[]")
    # A name (tailnet or LAN DNS) survives address changes better than an IP; IPv6 last.
    host = next((a for a in addrs if any(c.isalpha() for c in a) and ":" not in a), None) \
        or next((a for a in addrs if ":" not in a), None) or m["hostname"] or m["name"]
    return {"url": f"http://{host}:{o['port']}", "models": o["models"]}


def _pw_mark(pw_hash: str) -> str:
    """Short fingerprint of a password hash, kept in the session: a new password signs out old sessions."""
    return hashlib.sha256((pw_hash or "").encode()).hexdigest()[:16]


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    s = settings or load()
    if not s.admin_token:
        raise SystemExit("BUNCH_ADMIN_TOKEN is not set. Put a long random value in .env (see README).")
    if s.human_name in RESERVED_NAMES:
        raise SystemExit(f"BUNCH_HUMAN_NAME can't be '{s.human_name}': the hub posts its own notes under that name.")
    store = Store(s.db_path)
    store.ensure_human(s.human_name)
    llm = llm or LLM(s.litellm_url, s.litellm_key, think_max_tokens=s.think_max_tokens)
    hub = Hub(store, s, llm)

    # SVG is shown too: the viewer only puts files in <img> (no scripts there), and opened directly the
    # CSP below sandboxes it, so a script in one can't run on this origin.
    INLINE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}

    def retention_days() -> float:
        try:
            return max(0.0, float(store.setting("file_retention_days", "0")))
        except ValueError:
            return 0.0

    async def file_sweeper():
        while True:
            try:
                days = retention_days()
                if days:
                    n, _ = store.delete_files(older_than=time.time() - days * 86400)
                    if n:
                        log.info("file retention: removed %d files older than %g days", n, days)
            except Exception as e:  # e.g. database locked: try again next hour
                log.warning("file retention sweep failed: %s", e)
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hub._event = asyncio.Event()  # bind to the running loop
        tasks = []
        if s.summary_model and llm.enabled:
            tasks.append(asyncio.create_task(summarizer_loop(hub)))
        else:
            log.info("summarizer off (set BUNCH_SUMMARY_MODEL to enable)")
        if llm.enabled:
            tasks.append(asyncio.create_task(autoreply_loop(hub)))
        tasks.append(asyncio.create_task(file_sweeper()))
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="bunch", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    secret = s.session_secret or hmac.new(s.admin_token.encode(), b"bunch-session", hashlib.sha256).hexdigest()
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="bunch_session",
                       max_age=30 * 86400, same_site="lax", https_only=s.secure_cookies)
    failed_logins: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- auth
    def agent_of(authorization: str | None) -> str:
        row = store.agent_by_key(_bearer(authorization))
        if not row:
            raise HTTPException(401, "invalid or missing agent key", headers={"WWW-Authenticate": "Bearer"})
        return row["name"]

    def machine_of(authorization: str | None):
        row = store.machine_by_key(_bearer(authorization))
        if not row:
            raise HTTPException(401, "invalid or missing machine key", headers={"WWW-Authenticate": "Bearer"})
        return row

    def machine_agent(m, agent: str):
        """The agent, if it's assigned to machine m; a machine can only speak for its own agents."""
        a = store.agent(agent)
        if not a or a["kind"] != "agent" or a["model"] or hub.worker_config(a).get("machine") != m["name"]:
            raise HTTPException(403, f"{agent} isn't on {m['name']}")
        store.x("UPDATE agents SET last_seen=? WHERE name=?", (time.time(), agent))
        return a

    def is_admin(token: str) -> bool:
        return bool(token) and hmac.compare_digest(token.encode(), s.admin_token.encode())

    def session_user(session) -> tuple[str, bool] | None:
        row = store.agent(session.get("user", "")) if session.get("user") else None
        if row and row["kind"] == "human" and row["pw_hash"] and session.get("pw") == _pw_mark(row["pw_hash"]):
            return row["name"], bool(row["is_admin"])
        return None

    def viewer(request: Request, authorization: str | None) -> tuple[str, bool]:
        """(name, is_admin) of the human using the web viewer: a signed-in user or the admin token."""
        if is_admin(_bearer(authorization)):
            return s.human_name, True
        who = session_user(request.session)
        if not who:
            raise HTTPException(401, "sign in required", headers={"WWW-Authenticate": "Bearer"})
        return who

    def require_admin(request: Request, authorization: str | None) -> str:
        name, admin = viewer(request, authorization)
        if not admin:
            raise HTTPException(403, "admins only")
        return name

    @app.exception_handler(HubError)
    async def _hub_error(_req: Request, exc: HubError):
        return PlainTextResponse(f"error: {exc}", status_code=400)

    # -------------------------------------------------------------- basics
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    # Theme assets (CSS, fonts, fruit artwork), shared look with fruit-fs.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---------------------------------------------------------------- login
    def check_login_rate(request: Request) -> list[float]:
        """Recent failed password checks from this address; 429 after 10 in 5 minutes."""
        ip = request.client.host if request.client else "?"
        now = time.time()
        for old in [k for k, ts in failed_logins.items() if ts[-1] < now - 300]:  # keep the dict small
            del failed_logins[old]
        recent = [t for t in failed_logins.get(ip, []) if t > now - 300]
        if len(recent) >= 10:
            raise HTTPException(429, "too many failed sign-ins; try again in a few minutes")
        return recent

    async def failed_login(request: Request, recent: list[float]) -> None:
        failed_logins[request.client.host if request.client else "?"] = recent + [time.time()]
        await asyncio.sleep(0.5)

    @app.post("/api/auth/login")
    async def login(body: LoginIn, request: Request):
        recent = check_login_rate(request)
        row = store.verify_user(body.name, body.password)
        if not row:
            await failed_login(request, recent)
            raise HTTPException(401, "wrong name or password")
        failed_logins.pop(request.client.host if request.client else "?", None)
        row = store.agent(row["name"])  # verify_user may have re-hashed the password
        request.session["user"] = row["name"]
        request.session["pw"] = _pw_mark(row["pw_hash"])
        return {"name": row["name"], "admin": bool(row["is_admin"])}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request, authorization: str | None = Header(None)):
        name, admin = viewer(request, authorization)
        row = store.agent(name)
        return {"name": name, "admin": admin, "avatar": (row["avatar"] if row else None) or DEFAULT_HUMAN_AVATAR,
                "avatars": HUMAN_AVATARS}

    @app.put("/api/auth/me/avatar")
    async def set_my_avatar(body: AvatarIn, request: Request, authorization: str | None = Header(None)):
        """A person picks their own profile picture (fruit or vegetable). Agents keep theirs."""
        name, _ = viewer(request, authorization)
        try:
            store.set_avatar(name, body.avatar)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        hub.broadcast({"type": "agents"})  # everyone's viewer redraws this person's picture
        return {"name": name, "avatar": store.agent(name)["avatar"] or DEFAULT_HUMAN_AVATAR}

    # ----------------------------------------------------------------- MCP
    @app.post("/mcp")
    async def mcp_post(request: Request, authorization: str | None = Header(None)):
        agent = agent_of(authorization)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}},
                                status_code=400)
        resp = await mcpmod.handle(hub, agent, payload)
        if resp is None:
            return Response(status_code=202)
        return JSONResponse(resp)

    @app.get("/mcp")
    @app.delete("/mcp")
    async def mcp_no_stream():
        # Stateless server: no server-initiated SSE stream, no sessions to delete.
        return Response(status_code=405, headers={"Allow": "POST"})

    # ------------------------------------------- agent REST (same text output as MCP)
    @app.get("/api/v1/read")
    async def api_read(room: str | None = None, all: bool = False, wait: float = 0, format: str = "text",
                       authorization: str | None = Header(None)):
        agent = agent_of(authorization)
        deadline = time.monotonic() + max(0.0, min(wait, 60.0))
        while True:
            if format == "json":
                header, rows, more = hub.read_rows(agent, room, all)
                if rows or header or time.monotonic() >= deadline:
                    out = {"header": header, "more": more,
                           "messages": [{**hub.message_json(m), "for_me": agent in hub.message_json(m)["to"],
                                         "size": len(m["body"] or "")} for m in rows]}
                    for m, row in zip(out["messages"], rows):
                        m.pop("body")
                        if m["for_me"]:  # what responder.py needs to answer in context
                            m["recent"] = hub.recent_lines(m["room"], m["id"])
                            m["wake"] = bool(row["human"]) or hub.wakes(row, agent)
                            m["note"] = hub.room_note(m["room"])
                    store.add_usage(agent, ctx=sum(len(m["summary"]) for m in out["messages"]) // 4)
                    return out
            else:
                text = hub.read(agent, room, all)
                if text != "no new messages" or time.monotonic() >= deadline:
                    return PlainTextResponse(text)
            await hub.wait_for_new(max(0.05, deadline - time.monotonic()))

    @app.post("/api/v1/post")
    async def api_post(body: PostIn, authorization: str | None = Header(None)):
        return PlainTextResponse(hub.post(agent_of(authorization), body.room, body.text, to=body.to,
                                          summary=body.summary, reply_to=body.re))

    @app.get("/api/v1/get/{mid}")
    async def api_get(mid: int, offset: int = 0, authorization: str | None = Header(None)):
        return PlainTextResponse(hub.get(agent_of(authorization), mid, offset))

    @app.post("/api/v1/ask")
    async def api_ask(body: AskIn, authorization: str | None = Header(None)):
        agent = agent_of(authorization)
        return PlainTextResponse(await hub.ask(agent, body.model, body.prompt, body.system,
                                               body.max_tokens, body.room))

    @app.get("/api/v1/whoami")
    async def api_whoami(authorization: str | None = Header(None)):
        return PlainTextResponse(agent_of(authorization))

    # ------------------------------------------------ machines (clients/worker.py)
    @app.post("/api/v1/machine/hello")
    async def machine_hello(body: HelloIn, authorization: str | None = Header(None)):
        m = machine_of(authorization)
        power = body.max_power if body.max_power in WORKER_POWERS else "answer"
        clis = [{"cli": str(c.get("cli", ""))[:16], "version": str(c.get("version", ""))[:64],
                 "models": [str(x)[:64] for x in (c.get("models") or [])][:50]}
                for c in body.clis[:10] if c.get("cli") in WORKER_CLIS]
        ollama = None
        if isinstance(body.ollama, dict) and isinstance(body.ollama.get("models"), list):
            try:
                port = max(1, min(65535, int(body.ollama.get("port") or 11434)))
            except (TypeError, ValueError):
                port = 11434
            ollama = {"port": port, "models": sorted({str(t)[:100] for t in body.ollama["models"][:200] if t})}
        store.set_machine_report(m["name"], body.hostname[:64], [a[:64] for a in body.addresses[:20]], clis,
                                 power, [d[:512] for d in body.dirs[:20]], ollama)
        hub.broadcast({"type": "agents"})
        return {"machine": m["name"],
                "agents": [{"name": a["name"], **hub.worker_config(a)} for a in hub.machine_agents(m["name"])]}

    @app.get("/api/v1/machine/read")
    async def machine_read(request: Request, wait: float = 0, authorization: str | None = Header(None)):
        """New messages for this machine's agents, each with what answering it needs."""
        m = machine_of(authorization)
        deadline = time.monotonic() + max(0.0, min(wait, 60.0))
        while True:
            if await request.is_disconnected():
                return Response(status_code=499)  # worker restarted/crashed: leave the messages for its next read
            jobs = []
            for a in hub.machine_agents(m["name"]):
                name = a["name"]
                _h, rows, _more = hub.read_rows(name)
                held = [store.message(i) for i in hub.held.pop(name, {})]  # read before, waiting for their turn
                for r in [x for x in held if x] + rows:
                    if f",{name}," not in (r["recipients"] or ""):
                        continue  # room chatter: workers only answer what's addressed to them
                    if store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (r["id"], name)):
                        continue  # already answered, e.g. the hub said the machine was offline
                    if hub.duplicate_of(r) or (not r["human"] and not hub.wakes(r, name)):
                        continue  # a double send, or another agent only naming this one / chain too deep
                    if hub.turn_blocker(r, name):
                        hub.held.setdefault(name, {})[r["id"]] = None  # the cursor moved on: keep it here
                        continue
                    why = hub.over_budget(name)
                    if why:  # the hub enforces budgets, so a restarted worker can't run past one
                        try:
                            hub.post(name, r["room"], f"({why}; not answered)", reply_to=r["id"])
                        except HubError:
                            pass  # rate limit / paused room: the note is a courtesy
                        continue
                    parent = store.message(r["reply_to"]) if r["reply_to"] else None
                    jobs.append({"agent": name, "id": r["id"], "room": r["room"], "sender": r["sender"],
                                 "human": bool(r["human"]), "created": r["created"],
                                 "text": r["body"] or r["summary"], "room_summary": hub.room_summary(r["room"]),
                                 "recent": hub.recent_lines(r["room"], r["id"]), "note": hub.room_note(r["room"]),
                                 "parent": {"id": parent["id"], "sender": parent["sender"],
                                            "text": parent["body"] or parent["summary"]} if parent else None})
            if jobs or time.monotonic() >= deadline:
                return {"jobs": jobs}
            await hub.wait_for_new(max(0.05, deadline - time.monotonic()))

    @app.post("/api/v1/machine/post")
    async def machine_post(body: MachinePostIn, authorization: str | None = Header(None)):
        a = machine_agent(machine_of(authorization), body.agent)
        return PlainTextResponse(hub.post(a["name"], body.room, body.text, reply_to=body.re))

    @app.post("/api/v1/machine/file")
    async def machine_file(request: Request, agent: str, room: str, name: str = "file",
                           authorization: str | None = Header(None)):
        """A worker attaches a file (e.g. a generated image) to a room; the reply cites it as ![name](file:ID)."""
        a = machine_agent(machine_of(authorization), agent)
        room = room.strip().lower().lstrip("#")
        if not store.room(room):
            raise HTTPException(404, "no such room")
        data = await request.body()
        if not data or len(data) > s.max_file:
            raise HTTPException(413, f"file must be 1..{s.max_file} bytes")
        mime = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()
        name = "".join(c for c in name if c.isprintable() and c not in '/\\')[:80] or "file"
        fid = store.add_file(room, name, mime, data, a["name"])
        return {"id": fid, "markdown": f"![{name.replace(']', '')}](file:{fid})"}

    @app.post("/api/v1/machine/report")
    async def machine_report(body: ReportIn, authorization: str | None = Header(None)):
        a = machine_agent(machine_of(authorization), body.agent)
        return PlainTextResponse(hub.worker_report(a["name"], body.status, body.tokens, body.calls,
                                                   body.until, body.room, body.cached))

    @app.get("/api/v1/rooms")
    async def api_rooms(authorization: str | None = Header(None)):
        return PlainTextResponse(hub.rooms(agent_of(authorization)))

    # --------------------------------------------- admin / web viewer (full detail)
    # Chat routes (rooms, messages, agent list) are open to any signed-in human;
    # agent keys, budgets and users are admin-only.
    @app.get("/api/admin/rooms")
    async def admin_rooms(request: Request, authorization: str | None = Header(None)):
        viewer(request, authorization)
        out = []
        for r in store.rooms():
            last = store.one("SELECT MAX(id) AS id, COUNT(*) AS n FROM messages WHERE room=?", (r["name"],))
            out.append({"name": r["name"], "summary": hub.room_summary(r["name"], fallback=False),
                        "count": last["n"], "last_id": last["id"] or 0, "paused": hub.paused(r["name"]),
                        "bot": bool(r["bot"])})
        return out

    @app.get("/api/admin/rooms/{room}/messages")
    async def admin_messages(request: Request, room: str, before: int | None = None, after: int | None = None,
                             limit: int = 100, authorization: str | None = Header(None)):
        viewer(request, authorization)
        limit = max(1, min(limit, 500))
        if after is not None:  # catch-up after a reconnect: oldest first
            rows = store.q("SELECT * FROM messages WHERE room=? AND id>? ORDER BY id LIMIT ?", (room, after, limit))
            return [hub.message_json(m) for m in rows]
        rows = store.q("SELECT * FROM messages WHERE room=? AND id<? ORDER BY id DESC LIMIT ?",
                       (room, before or 2**62, limit))
        return [hub.message_json(m) for m in rows[::-1]]

    @app.post("/api/admin/rooms/{room}/messages")
    async def admin_post(request: Request, room: str, body: PostIn, authorization: str | None = Header(None)):
        name, _ = viewer(request, authorization)
        to = body.to
        known = store.agent_names()
        named = [n.rstrip(".") for n in MENTION_RE.findall((body.text or "").lower())]
        addressed = to or body.re is not None or any(n in known for n in named)
        if not addressed and (body.text or "").strip():
            to = await route(hub, room.strip().lower().lstrip("#"), body.text, sender=name) or None
        return {"result": hub.post(name, room, body.text, to=to, summary=body.summary,
                                   reply_to=body.re, human=True),
                "unknown": sorted({n for n in named if n not in known})}  # a typo'd @ otherwise asks nobody

    @app.get("/api/admin/search")
    async def admin_search(request: Request, q: str = "", room: str | None = None, limit: int = 50,
                           authorization: str | None = Header(None)):
        viewer(request, authorization)
        return hub.search(q, room, limit)

    @app.put("/api/admin/rooms/{room}/bot")
    async def admin_room_bot(request: Request, room: str, body: BotIn, authorization: str | None = Header(None)):
        """Bot mode: agents in this room hand work to each other without a person, until one posts [done]."""
        require_admin(request, authorization)
        room = room.strip().lower().lstrip("#")
        if not store.room(room):
            raise HTTPException(404, "no such room")
        store.set_room_bot(room, body.on)
        hub.broadcast({"type": "rooms"})
        return {"room": room, "bot": body.on}

    @app.delete("/api/admin/rooms/{room}")
    async def admin_delete_room(request: Request, room: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        room = room.strip().lower().lstrip("#")
        n = store.delete_room(room)
        if n is None:
            raise HTTPException(404, "no such room")
        hub.broadcast({"type": "room_deleted", "room": room})
        return {"deleted": room, "messages": n}

    @app.get("/api/admin/files/{fid}")
    async def admin_file(request: Request, fid: int, authorization: str | None = Header(None)):
        viewer(request, authorization)
        row = store.file(fid)
        if not row:
            raise HTTPException(404, "no such file")
        inline = row["mime"] in INLINE_IMAGES  # anything else (HTML...) downloads instead of running here
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600",
                   "Content-Security-Policy": "default-src 'none'; sandbox"}
        if not inline:
            ascii_name = row["name"].encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
            headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(row["name"])}'
        return FileResponse(store.file_path(fid), media_type=row["mime"] if inline else "application/octet-stream",
                            headers=headers)

    @app.get("/api/admin/files")
    async def admin_files(request: Request, room: str | None = None, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        rows = store.q("SELECT * FROM files" + (" WHERE room=?" if room else "") + " ORDER BY id DESC LIMIT 1000",
                       (room,) if room else ())
        per_room = store.q("SELECT room, COUNT(*) AS n, SUM(size) AS bytes FROM files GROUP BY room ORDER BY room")
        return {"retention_days": retention_days(), "max_file": s.max_file,
                "rooms": [dict(r) for r in per_room], "files": [dict(r) for r in rows]}

    @app.put("/api/admin/files/retention")
    async def admin_files_retention(request: Request, body: RetentionIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        days = max(0.0, body.days)
        store.set_setting("file_retention_days", repr(days))
        n = 0
        if days:
            n, _ = store.delete_files(older_than=time.time() - days * 86400)
        return {"retention_days": days, "deleted": n}

    @app.post("/api/admin/files/purge")
    async def admin_files_purge(request: Request, body: PurgeIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        cutoff = time.time() - body.older_than_days * 86400 if body.older_than_days is not None else None
        if body.ids is None and not body.room and cutoff is None:
            raise HTTPException(400, "say which files: ids, room and/or older_than_days")
        n, freed = store.delete_files(ids=body.ids, room=(body.room or "").strip().lower().lstrip("#") or None,
                                      older_than=cutoff)
        return {"deleted": n, "bytes": freed}

    @app.delete("/api/admin/messages/{mid}")
    async def admin_delete_message(request: Request, mid: int, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        row = store.delete_message(mid)
        if not row:
            raise HTTPException(404, "no such message")
        hub.broadcast({"type": "deleted", "id": mid, "room": row["room"]})
        return {"deleted": mid}

    @app.get("/api/admin/agents")
    async def admin_agents(request: Request, authorization: str | None = Header(None)):
        _, admin = viewer(request, authorization)
        await hub.refresh_model_hosts()
        out = []
        first = store.setting("first_responder")
        for a in store.agents():
            u = store.usage_today(a["name"])
            out.append({"name": a["name"], "kind": a["kind"], "last_seen": a["last_seen"],
                        "description": a["description"], "model": a["model"],
                        "daily_tokens": hub.budget(a["name"]) if a["kind"] == "agent" else None,
                        "budget_set": a["daily_tokens"], "system": a["system"] if admin else None,
                        "ask_tokens_today": u["ask"], "ctx_tokens_today": u["ctx"],
                        "sub_tokens_today": u["sub"], "sub_calls_today": u["calls"],
                        "sub_cached_today": u["cached"], "spent_today": hub.spent(a["name"]),
                        "state": hub.worker_state(a), "status_until": a["status_until"],
                        "worker": _worker_view(hub.worker_config(a), admin),
                        "runs_as": hub.runs_as(a),
                        "runs_on": hub.model_host(a["model"]) if a["model"] else None,
                        "machine": hub.agent_machine(a), "first_responder": a["name"] == first,
                        "avatar": (a["avatar"] or DEFAULT_HUMAN_AVATAR) if a["kind"] == "human" else None})
        # The hub's own helpers are listed only while they're configured or have used tokens today.
        for system, model, does in (("_summarizer", hub.s.summary_model, "writes the room summaries"),
                                    ("_router", hub.s.router_model, "picks who answers")):
            u = store.usage_today(system)
            if model or u["ask"]:
                out.append({"name": system, "kind": "system", "last_seen": None, "description": does,
                            "model": model or None, "daily_tokens": None,
                            "runs_on": hub.model_host(model) if model else None, "machine": hub.model_machine(model),
                            "ask_tokens_today": u["ask"], "ctx_tokens_today": 0})
        return out

    @app.get("/api/admin/models")
    async def admin_models(request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not llm.enabled:
            return []
        try:
            names = await llm.models()
        except LLMError as e:
            raise HTTPException(502, str(e))
        await hub.refresh_model_hosts()
        try:
            deps = await llm.model_info()
        except (LLMError, AttributeError):  # older LiteLLM / key without access: list only
            deps = []
        stored = {d["name"] for d in deps if d["stored"]}
        return [{"name": n, "runs_on": hub.model_host(n), "machine": hub.model_machine(n),
                 "stored": n in stored, "used_by": model_users(n),
                 "gone": any(model_gone(d) for d in deps if d["name"] == n)} for n in names]

    def model_gone(d: dict) -> bool:
        """An Ollama model on a machine that shares its list, but that machine no longer has it."""
        tag = d["model"].split("/", 1)[1] if d["model"].startswith(("ollama/", "ollama_chat/")) else None
        for m in store.machines():
            o = ollama_share(m)
            if tag and o and d["api_base"].rstrip("/") == o["url"]:
                return tag not in o["models"]
        return False

    def model_users(name: str) -> list[str]:
        """Who answers with a gateway model: agents, and the hub's own summarizer and router."""
        out = [a["name"] for a in store.agents() if a["model"] == name]
        return out + [sys for sys, m in (("_summarizer", s.summary_model), ("_router", s.router_model)) if m == name]

    async def check_model(name: str) -> None:
        try:
            await llm.chat(name, [{"role": "user", "content": "Reply with OK."}], 5)
        except LLMError as e:
            raise HTTPException(502, f"{name} didn't answer through the gateway: {e}. If it's an Ollama server on "
                                     "another machine, it needs OLLAMA_HOST=0.0.0.0 and its port (11434) open "
                                     "to the gateway.")

    async def stored_deployments(name: str) -> list[dict]:
        try:
            deps = [m for m in await llm.model_info() if m["name"] == name]
        except LLMError as e:
            raise HTTPException(502, str(e))
        if not deps:
            raise HTTPException(404, "no such model")
        if not any(d["stored"] for d in deps):
            raise HTTPException(400, f"{name} is in litellm/config.yaml; change it there")
        return [d for d in deps if d["stored"]]

    def models_changed() -> None:
        hub._hosts_at = 0.0  # re-read hosts on the next look
        hub.broadcast({"type": "agents"})

    @app.post("/api/admin/models")
    async def admin_add_model(request: Request, body: ModelIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        name, model, base = body.name.strip(), body.model.strip(), (body.api_base or "").strip() or None
        if not MODEL_NAME_RE.fullmatch(name) or not model or len(model) > 200:
            raise HTTPException(400, "name: letters, digits and . _ - / : (up to 64); model required")
        if base and not base.startswith(("http://", "https://")):
            raise HTTPException(400, "server address must start with http:// or https://")
        try:
            if name in await llm.models():
                raise HTTPException(409, f"the gateway already has {name}")
            await llm.add_model(name, model, base)
        except LLMError as e:
            raise HTTPException(502, str(e))
        if body.check:
            try:
                await check_model(name)
            except HTTPException:
                for d in [m for m in await llm.model_info() if m["name"] == name and m["stored"]]:
                    await llm.delete_model(d["id"])  # don't keep a model nobody can reach
                raise
        models_changed()
        return {"name": name}

    @app.post("/api/admin/models/test")
    async def admin_test_model(request: Request, body: ModelRefIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        await check_model(body.name)
        return {"name": body.name, "ok": True}

    @app.post("/api/admin/models/delete")
    async def admin_delete_model(request: Request, body: ModelRefIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        deps = await stored_deployments(body.name)
        users = model_users(body.name)
        if users and not body.force:
            raise HTTPException(409, f"{', '.join(users)} use{'s' if len(users) == 1 else ''} {body.name}")
        try:
            for d in deps:
                await llm.delete_model(d["id"])
        except LLMError as e:
            raise HTTPException(502, str(e))
        models_changed()
        return {"deleted": body.name, "used_by": users}

    @app.post("/api/admin/models/rename")
    async def admin_rename_model(request: Request, body: ModelRefIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        to = (body.to or "").strip()
        if not MODEL_NAME_RE.fullmatch(to):
            raise HTTPException(400, "new name: letters, digits and . _ - / : (up to 64)")
        deps = await stored_deployments(body.name)
        if set(model_users(body.name)) & {"_summarizer", "_router"}:
            raise HTTPException(409, f"{body.name} is set in .env (BUNCH_SUMMARY_MODEL / BUNCH_ROUTER_MODEL); "
                                     "change it there first")
        try:
            if to in await llm.models():
                raise HTTPException(409, f"the gateway already has {to}")
            for d in deps:
                await llm.add_model(to, d["model"], d["api_base"] or None)
            for d in deps:
                await llm.delete_model(d["id"])
        except LLMError as e:
            raise HTTPException(502, str(e))
        moved = model_users(body.name)
        for a in moved:
            store.set_agent_info(a, model=to)
        models_changed()
        return {"name": to, "agents_moved": moved}

    @app.post("/api/admin/agents")
    async def admin_add_agent(request: Request, body: AgentIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        name = body.name.strip().lower()
        cfg = hub.clean_worker(body.worker.model_dump()) if body.worker else None  # validate before creating
        model = None if cfg else (body.model or "").strip() or None
        try:
            key = store.create_agent(name, body.daily_tokens, body.description.strip(),
                                     model, (body.system or "").strip() or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if cfg:
            store.set_worker(name, cfg)
            hub.broadcast({"type": "agents"})
            return {"name": name, "machine": cfg["machine"]}  # its machine speaks for it: no key to hand out
        return {"name": name, "key": key, "note": "shown once; store it on the agent's machine"}

    @app.put("/api/admin/agents/{name}/info")
    async def admin_agent_info(request: Request, name: str, body: AgentInfoIn,
                               authorization: str | None = Header(None)):
        require_admin(request, authorization)
        row = store.agent(name)
        if not row or row["kind"] != "agent":
            raise HTTPException(404, "no such agent")
        fields = body.model_dump(exclude_none=True)
        store.set_agent_info(name, **fields)
        if (fields.get("model") or "").strip():
            store.set_worker(name, None)  # an auto agent is answered by the hub, not a worker
        row = store.agent(name)
        return {"name": name, "description": row["description"], "model": row["model"], "system": row["system"]}

    @app.put("/api/admin/agents/{name}/worker")
    async def admin_agent_worker(request: Request, name: str, body: WorkerIn,
                                 authorization: str | None = Header(None)):
        require_admin(request, authorization)
        row = store.agent(name)
        if not row or row["kind"] != "agent":
            raise HTTPException(404, "no such agent")
        cfg = hub.clean_worker(body.model_dump())
        store.set_worker(name, cfg)
        if cfg:
            store.set_agent_info(name, model=None)  # one answerer per agent: the worker, not the hub
        hub.broadcast({"type": "agents"})
        return hub.worker_config(store.agent(name))

    @app.get("/api/admin/machines")
    async def admin_machines(request: Request, authorization: str | None = Header(None)):
        _, admin = viewer(request, authorization)
        return [_machine_json(hub, m, admin) for m in store.machines()]

    @app.post("/api/admin/machines")
    async def admin_add_machine(request: Request, body: MachineIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        name = body.name.strip().lower()
        try:
            key = store.create_machine(name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        hub.broadcast({"type": "agents"})
        return {"name": name, "key": key, "note": "shown once; run the install command on that machine"}

    @app.post("/api/admin/machines/{name}/rotate")
    async def admin_rotate_machine(request: Request, name: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not store.machine(name):
            raise HTTPException(404, "no such machine")
        return {"name": name, "key": store.rotate_machine_key(name)}

    @app.delete("/api/admin/machines/{name}")
    async def admin_delete_machine(request: Request, name: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        agents = [a["name"] for a in hub.machine_agents(name)]
        if not store.delete_machine(name):
            raise HTTPException(404, "no such machine")
        for a in agents:  # they stay, but nothing runs them until they're moved
            store.set_worker(a, None)
        hub.broadcast({"type": "agents"})
        return {"deleted": name, "agents_unassigned": agents}

    # ---------------------------------------------------------------- users
    @app.get("/api/admin/users")
    async def admin_users(request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        return [{"name": u["name"], "admin": bool(u["is_admin"]), "last_seen": u["last_seen"]} for u in store.users()]

    @app.post("/api/admin/users")
    async def admin_add_user(request: Request, body: UserIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        name = body.name.strip().lower()
        try:
            store.create_user(name, body.password, body.admin)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": name, "admin": body.admin}

    @app.put("/api/admin/users/{name}/password")
    async def admin_user_password(request: Request, name: str, body: PasswordIn,
                                  authorization: str | None = Header(None)):
        who, admin = viewer(request, authorization)
        if who != name and not admin:
            raise HTTPException(403, "admins only")
        if who == name and not is_admin(_bearer(authorization)):
            # Your own password from a session: prove it's you, so a borrowed or stolen session can't lock you out.
            recent = check_login_rate(request)
            if not store.verify_user(name, body.current_password or ""):
                await failed_login(request, recent)
                raise HTTPException(403, "current password is wrong")  # not 401: that would sign the viewer out
        try:
            store.set_password(name, body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if who == name and not is_admin(_bearer(authorization)):  # keep the person who changed it signed in
            request.session["pw"] = _pw_mark(store.agent(name)["pw_hash"])
        return {"name": name}

    @app.put("/api/admin/users/{name}/admin")
    async def admin_user_flag(request: Request, name: str, body: AdminFlagIn,
                              authorization: str | None = Header(None)):
        require_admin(request, authorization)
        try:
            # signed-in admins can't demote the last admin; the admin token can never be locked out
            store.set_admin(name, body.admin, keep_one=not is_admin(_bearer(authorization)))
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": name, "admin": body.admin}

    @app.delete("/api/admin/users/{name}")
    async def admin_delete_user(request: Request, name: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        try:
            deleted = store.delete_user(name, keep_one=not is_admin(_bearer(authorization)))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not deleted:
            raise HTTPException(404, "no such user")
        return {"deleted": name}

    @app.post("/api/admin/agents/{name}/rotate")
    async def admin_rotate(request: Request, name: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        try:
            return {"name": name, "key": store.rotate_key(name)}
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.put("/api/admin/agents/{name}/budget")
    async def admin_budget(request: Request, name: str, body: BudgetIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not store.agent(name):
            raise HTTPException(404, "no such agent")
        store.set_budget(name, body.daily_tokens)
        return {"name": name, "daily_tokens": hub.budget(name)}

    @app.put("/api/admin/agents/{name}/first")
    async def admin_first(request: Request, name: str, body: FirstIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        row = store.agent(name)
        if not row or row["kind"] != "agent":
            raise HTTPException(404, "no such agent")
        if body.on:
            store.set_setting("first_responder", name)
        elif store.setting("first_responder") == name:
            store.set_setting("first_responder", "")
        return {"first_responder": store.setting("first_responder") or None}

    @app.delete("/api/admin/agents/{name}")
    async def admin_delete(request: Request, name: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not store.delete_agent(name):
            raise HTTPException(404, "no such agent")
        return {"deleted": name}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        try:  # first frame must be {"token": "..."} so the token never sits in a URL or log
            hello = await asyncio.wait_for(socket.receive_json(), timeout=10)
        except Exception:
            await socket.close(code=4401)
            return
        origin = socket.headers.get("origin", "")
        same_origin = not origin or origin.split("://", 1)[-1] == socket.headers.get("host", "")
        if not (is_admin(str(hello.get("token", ""))) or (same_origin and session_user(socket.session))):
            await socket.close(code=4401)
            return
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        hub.listeners.add(q)
        try:
            await socket.send_json({"type": "ready"})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    event = {"type": "ping"}
                await socket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.listeners.discard(q)

    return app
