"""HTTP server: MCP endpoint, agent REST API, admin API, web viewer."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
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
from .hub import MENTION_RE, Hub, HubError
from .llm import LLM, LLMError
from .router import route
from .store import Store
from .summarizer import summarizer_loop

log = logging.getLogger("bunch")
STATIC = Path(__file__).parent / "static"


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


class AgentIn(BaseModel):
    name: str
    daily_tokens: int | None = None
    description: str = ""
    model: str | None = None
    system: str | None = None


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


class PasswordIn(BaseModel):
    password: str


class BudgetIn(BaseModel):
    daily_tokens: int | None = None


def _bearer(value: str | None) -> str:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""


def create_app(settings: Settings | None = None, llm: LLM | None = None) -> FastAPI:
    s = settings or load()
    if not s.admin_token:
        raise SystemExit("BUNCH_ADMIN_TOKEN is not set. Put a long random value in .env (see README).")
    store = Store(s.db_path)
    store.ensure_human(s.human_name)
    llm = llm or LLM(s.litellm_url, s.litellm_key)
    hub = Hub(store, s, llm)

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
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="bunch", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    secret = s.session_secret or hmac.new(s.admin_token.encode(), b"bunch-session", hashlib.sha256).hexdigest()
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="bunch_session",
                       max_age=30 * 86400, same_site="lax", https_only=False)
    failed_logins: dict[str, list[float]] = {}

    # ---------------------------------------------------------------- auth
    def agent_of(authorization: str | None) -> str:
        row = store.agent_by_key(_bearer(authorization))
        if not row:
            raise HTTPException(401, "invalid or missing agent key", headers={"WWW-Authenticate": "Bearer"})
        return row["name"]

    def is_admin(token: str) -> bool:
        return bool(token) and hmac.compare_digest(token.encode(), s.admin_token.encode())

    def session_user(session) -> tuple[str, bool] | None:
        row = store.agent(session.get("user", "")) if session.get("user") else None
        if row and row["kind"] == "human" and row["pw_hash"]:
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
    @app.post("/api/auth/login")
    async def login(body: LoginIn, request: Request):
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in failed_logins.get(ip, []) if t > now - 300]
        if len(recent) >= 10:
            raise HTTPException(429, "too many failed sign-ins; try again in a few minutes")
        row = store.verify_user(body.name, body.password)
        if not row:
            failed_logins[ip] = recent + [now]
            await asyncio.sleep(0.5)
            raise HTTPException(401, "wrong name or password")
        failed_logins.pop(ip, None)
        request.session["user"] = row["name"]
        return {"name": row["name"], "admin": bool(row["is_admin"])}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/auth/me")
    async def me(request: Request, authorization: str | None = Header(None)):
        name, admin = viewer(request, authorization)
        return {"name": name, "admin": admin}

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
                    for m in out["messages"]:
                        m.pop("body")
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
            out.append({"name": r["name"], "summary": hub.room_summary(r["name"]),
                        "count": last["n"], "last_id": last["id"] or 0})
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
        addressed = to or body.re is not None or any(n.rstrip(".") in known for n in MENTION_RE.findall(body.text or ""))
        if not addressed and (body.text or "").strip():
            to = await route(hub, room.strip().lower().lstrip("#"), body.text) or None
        return {"result": hub.post(name, room, body.text, to=to, summary=body.summary,
                                   reply_to=body.re, human=True)}

    @app.get("/api/admin/agents")
    async def admin_agents(request: Request, authorization: str | None = Header(None)):
        viewer(request, authorization)
        out = []
        for a in store.agents():
            u = store.usage_today(a["name"])
            out.append({"name": a["name"], "kind": a["kind"], "last_seen": a["last_seen"],
                        "description": a["description"], "model": a["model"],
                        "daily_tokens": hub.budget(a["name"]) if a["kind"] == "agent" else None,
                        "ask_tokens_today": u["ask"], "ctx_tokens_today": u["ctx"]})
        for system in ("_summarizer", "_router"):
            u = store.usage_today(system)
            if system == "_summarizer" or u["ask"]:
                out.append({"name": system, "kind": "system", "last_seen": None, "description": "",
                            "model": None, "daily_tokens": None,
                            "ask_tokens_today": u["ask"], "ctx_tokens_today": 0})
        return out

    @app.get("/api/admin/models")
    async def admin_models(request: Request, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not llm.enabled:
            return []
        try:
            return await llm.models()
        except LLMError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/admin/agents")
    async def admin_add_agent(request: Request, body: AgentIn, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        name = body.name.strip().lower()
        try:
            key = store.create_agent(name, body.daily_tokens, body.description.strip(),
                                     (body.model or "").strip() or None, (body.system or "").strip() or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": name, "key": key, "note": "shown once; store it on the agent's machine"}

    @app.put("/api/admin/agents/{name}/info")
    async def admin_agent_info(request: Request, name: str, body: AgentInfoIn,
                               authorization: str | None = Header(None)):
        require_admin(request, authorization)
        row = store.agent(name)
        if not row or row["kind"] != "agent":
            raise HTTPException(404, "no such agent")
        store.set_agent_info(name, **body.model_dump(exclude_none=True))
        row = store.agent(name)
        return {"name": name, "description": row["description"], "model": row["model"], "system": row["system"]}

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
        try:
            store.set_password(name, body.password)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"name": name}

    @app.delete("/api/admin/users/{name}")
    async def admin_delete_user(request: Request, name: str, authorization: str | None = Header(None)):
        require_admin(request, authorization)
        if not store.delete_user(name):
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
