"""Minimal MCP server (Streamable HTTP transport, stateless, JSON responses).

Implemented by hand rather than with an SDK so the tool list stays tiny and
the wire format stays stable. Tool descriptions are deliberately terse:
MCP clients send every tool definition to the model on every turn."""
from __future__ import annotations

import time

from . import __version__
from .hub import Hub, HubError

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]

INSTRUCTIONS = ("bunch: shared chat for AI agents and humans. Call read for new messages, post to reply. "
                "Keep posts short; for long content pass a one-line summary.")

_obj = lambda props, req=(): {"type": "object", "properties": props, **({"required": list(req)} if req else {})}  # noqa: E731

TOOLS = [
    {"name": "read",
     "description": "New messages for you since last read (all rooms if room omitted). "
                    "Line: #id sender>to re#id: summary (+N chars, use get).",
     "inputSchema": _obj({"room": {"type": "string"},
                          "all": {"type": "boolean", "description": "include msgs addressed to others"},
                          "wait": {"type": "integer", "description": "seconds to block for new msgs (max 60)"}})},
    {"name": "post",
     "description": "Post to a room (created if new). @name addresses an agent; re=#id replies to its author.",
     "inputSchema": _obj({"room": {"type": "string"}, "text": {"type": "string"},
                          "summary": {"type": "string", "description": "one line, if text is long"},
                          "to": {"type": "string", "description": "names, comma-separated"},
                          "re": {"type": "integer"}}, ["room", "text"])},
    {"name": "get",
     "description": "Full text of message #id (paged).",
     "inputSchema": _obj({"id": {"type": "integer"}, "offset": {"type": "integer"}}, ["id"])},
    {"name": "ask",
     "description": "Run a prompt on any model via the gateway. model='?' lists models. "
                    "room=name posts the answer there and returns one line.",
     "inputSchema": _obj({"model": {"type": "string"}, "prompt": {"type": "string"},
                          "system": {"type": "string"}, "max_tokens": {"type": "integer"},
                          "room": {"type": "string"}}, ["model"])},
    {"name": "rooms",
     "description": "Rooms with unread counts and summaries, plus agents.",
     "inputSchema": _obj({})},
]


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


async def call_tool(hub: Hub, agent: str, name: str, a: dict) -> str:
    if name == "read":
        deadline = time.monotonic() + max(0.0, min(float(a.get("wait") or 0), 60.0))
        while True:
            text = hub.read(agent, a.get("room") or None, bool(a.get("all")))
            if text != "no new messages" or time.monotonic() >= deadline:
                return text
            await hub.wait_for_new(max(0.05, deadline - time.monotonic()))
    if name == "post":
        re_ = a.get("re")
        return hub.post(agent, a.get("room", ""), a.get("text", ""), to=a.get("to"),
                        summary=a.get("summary"), reply_to=int(re_) if re_ not in (None, "") else None)
    if name == "get":
        return hub.get(agent, int(a["id"]), int(a.get("offset") or 0))
    if name == "ask":
        mt = a.get("max_tokens")
        return await hub.ask(agent, a.get("model", ""), a.get("prompt"), a.get("system"),
                             int(mt) if mt not in (None, "") else None, a.get("room") or None)
    if name == "rooms":
        return hub.rooms(agent)
    raise HubError(f"unknown tool '{name}'")


async def handle_one(hub: Hub, agent: str, msg) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request")
    method, mid = msg.get("method"), msg.get("id")
    if method is None or mid is None:  # notification or a response: nothing to send back
        return None
    params = msg.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return _ok(mid, {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "bunch", "version": __version__},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            text, is_err = await call_tool(hub, agent, name, args), False
        except HubError as e:
            text, is_err = f"error: {e}", True
        except (KeyError, ValueError, TypeError) as e:
            text, is_err = f"error: bad arguments ({e})", True
        return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_err})
    if method in ("resources/list", "prompts/list", "resources/templates/list"):
        key = {"resources/list": "resources", "prompts/list": "prompts"}.get(method, "resourceTemplates")
        return _ok(mid, {key: []})
    return _err(mid, -32601, f"method not found: {method}")


async def handle(hub: Hub, agent: str, payload):
    if isinstance(payload, list):
        out = [r for r in [await handle_one(hub, agent, p) for p in payload] if r is not None]
        return out or None
    return await handle_one(hub, agent, payload)
