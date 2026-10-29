"""Background job: answers for "auto" agents (agents with a model set), so a model can
take part in the chat without running clients/responder.py anywhere.

Same rules as responder.py: each answer sees the room summary, one-line summaries of the
latest messages, the message and the message it replies to; humans are always answered
(but not twice for a double send), other agents only when they ask @<name> explicitly
("@name, can you…"), the chain below the human is still short and the human didn't already
ask this agent (loop guard). Agents in the same room answer one at a time, each seeing the
answers before it."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .hub import Hub, HubError

log = logging.getLogger("bunch.autoreply")

DEFAULT_SYSTEM = ("You are {name}, an AI agent in a shared chat room with other agents and humans. "
                  "Answer the message addressed to you. Be brief and concrete. "
                  "Recent messages are listed: build on what was already said instead of repeating it. "
                  "Only @mention another agent when you need it to act now; to point a human at an agent, "
                  "write its name without @.")
MAX_INPUT_CHARS = 12_000
_room_locks: dict[tuple[int, str], asyncio.Lock] = {}  # (event loop, room): auto agents answer one at a time
_gave_up: dict[tuple[str, int], float] = {}  # agent messages a model returned nothing for: don't retry every pass
STALE_AFTER = 300  # ignore messages older than this (e.g. backlog when an agent first becomes auto)
# Small models copy the notes they see in the room, e.g. "(empty answer)" or "(agy failed: ...)": that's no answer.
FILLER_RE = re.compile(r"^\((?:empty answer|[\w.:/-]+ (?:failed|returned nothing|didn't answer)\b[^\n]*)\)$", re.I)


def _text(m) -> str:
    return m["body"] or m["summary"]


async def answer(hub: Hub, agent, m) -> None:
    name, st = agent["name"], hub.store
    body = _text(m)
    if not m["human"] and not hub.wakes(m, name):
        return
    lock = _room_locks.setdefault((id(asyncio.get_running_loop()), m["room"]), asyncio.Lock())
    async with lock:  # one at a time: later ones see earlier answers
        await _answer(hub, agent, m, body)


async def _answer(hub: Hub, agent, m, body: str) -> None:
    name, st = agent["name"], hub.store
    if st.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (m["id"], name)):
        return  # answered while waiting for the room
    parts = [f"Room #{m['room']} summary: {hub.room_summary(m['room'])}"]
    recent = hub.recent_lines(m["room"], m["id"])
    if recent:
        parts.append(f"Recent messages:\n{recent}")
    if m["reply_to"]:
        parent = st.message(m["reply_to"])
        if parent:
            parts.append(f"It replies to #{parent['id']} from {parent['sender']}:\n"
                         f"{_text(parent)[:MAX_INPUT_CHARS // 3]}")
    parts.append(f"Message #{m['id']} from {m['sender']}:\n{body[:MAX_INPUT_CHARS]}")
    system = agent["system"] or DEFAULT_SYSTEM.format(name=name)
    hub.broadcast({"type": "typing", "agent": name, "room": m["room"]})  # viewer shows "thinking…"
    try:
        text = (await hub.ask(name, agent["model"], "\n\n".join(parts), system, hub.s.ask_max_tokens, raw=True)).strip()
    finally:
        hub.broadcast({"type": "typing", "agent": name, "room": m["room"], "done": True})
    if FILLER_RE.match(text):
        text = ""
    if not text:
        if not m["human"]:
            _gave_up[(name, m["id"])] = m["created"]
            return  # don't answer another agent with noise; they can ask again
        text = f"({agent['model']} returned nothing; try again or rephrase)"
    hub.post(name, m["room"], text, reply_to=m["id"])
    st.x("UPDATE agents SET last_seen=? WHERE name=?", (time.time(), name))


async def serve_agent(hub: Hub, agent) -> None:
    name = agent["name"]
    _header, rows, _more = hub.read_rows(name, None, False)
    offline = hub.worker_state(agent) == "offline"  # its model's machine is off: the hub already said so
    for m in rows:
        if f",{name}," not in (m["recipients"] or "") or m["created"] < time.time() - STALE_AFTER or offline:
            continue
        if (name, m["id"]) in _gave_up:
            continue
        if hub.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (m["id"], name)):
            continue  # already answered, e.g. with an "offline" note
        if hub.duplicate_of(m):
            continue  # a double send: the first copy is answered
        try:
            await answer(hub, agent, m)
        except HubError as e:  # gateway down, budget used: say so once, don't retry forever
            log.warning("%s could not answer #%s: %s", name, m["id"], e)
            try:
                hub.post(name, m["room"], f"({agent['model']} didn't answer: {str(e)[:160]})", reply_to=m["id"])
            except HubError:
                pass  # room paused / rate limit: nothing more to do


async def autoreply_once(hub: Hub) -> None:
    cutoff = time.time() - STALE_AFTER  # older messages are skipped anyway: forget them
    for k in [k for k, created in _gave_up.items() if created < cutoff]:
        del _gave_up[k]
    await hub.refresh_model_hosts()
    await asyncio.gather(*(serve_agent(hub, a) for a in hub.store.auto_agents()))


async def autoreply_loop(hub: Hub) -> None:
    log.info("auto agents on")
    while True:
        woke = hub._event  # grab before the pass so a message posted mid-pass isn't missed
        try:
            await autoreply_once(hub)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("auto-reply pass failed: %s", e)
        try:
            await asyncio.wait_for(woke.wait(), 30)
        except asyncio.TimeoutError:
            pass
