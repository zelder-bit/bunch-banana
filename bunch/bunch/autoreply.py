"""Background job: answers for "auto" agents (agents with a model set), so a model can
take part in the chat without running clients/responder.py anywhere.

Same rules as responder.py: each answer sees the room summary, one-line summaries of the
latest messages, the message and the message it replies to; humans are always answered
(but not twice for a double send), other agents only when they ask @<name> explicitly
("@name, can you…"), the chain below the human is still short and the human didn't already
ask this agent (loop guard). Agents in the same room answer one at a time, each seeing the
answers before it; with subscription workers asked in the same message, auto agents go first."""
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
                  "write its name without @. Write only your own reply, as plain text: never write lines "
                  "for other speakers or in the 'sender>to re#id:' format of the list.")
MAX_INPUT_CHARS = 12_000
_room_locks: dict[tuple[int, str], asyncio.Lock] = {}  # (event loop, room): auto agents answer one at a time


def forget_room(room: str) -> None:
    """A deleted room's lock would otherwise stay for good. One still held is left for its holder to finish."""
    for key in [k for k, lock in _room_locks.items() if k[1] == room and not lock.locked()]:
        del _room_locks[key]
_gave_up: dict[tuple[str, int], float] = {}  # agent messages a model returned nothing for: don't retry every pass
STALE_AFTER = 300  # ignore messages older than this (e.g. backlog when an agent first becomes auto)
# Small models copy the notes they see in the room, e.g. "(empty answer)" or "(agy failed: ...)": that's no answer.
FILLER_RE = re.compile(r"^\((?:empty answer(?::[^\n]*)?|[\w.:/-]+ (?:failed|returned nothing|didn't answer)\b[^\n]*)\)$", re.I)
# ...and the "Recent messages" lines: "#12 alice>bob re#11: text". A model that goes on in that format
# writes a whole made-up conversation, speaking for other agents and the human.
SPEAKER_RE = re.compile(r"^\s*(?:#\d+\s+)?@?([\w.-]+)(?:>[\w.,@-]+(?:\s+re#\d+)?|\s+re#\d+):\s*")


def _own_reply(text: str, name: str) -> str:
    """text up to the first line written as another message of the room; a leading header for the
    agent itself ("helper>human re#4: …") is dropped and the rest of that line kept."""
    lines = text.split("\n")
    first = SPEAKER_RE.match(lines[0])
    if first and first.group(1).lower() == name.lower():
        lines[0] = lines[0][first.end():]
    in_code = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and SPEAKER_RE.match(line):
            lines = lines[:i]
            break
    return "\n".join(lines).strip()


def _text(m) -> str:
    return m["body"] or m["summary"]


async def answer(hub: Hub, agent, m) -> None:
    name, st = agent["name"], hub.store
    body = _text(m)
    if not m["human"] and not hub.wakes(m, name):
        return
    await hub.wait_turn(m, name)  # agents asked together answer in turn, each seeing the earlier answers
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
    note = hub.room_note(m["room"])
    if note:  # bot mode: keep handing work on instead of waiting for the person
        system += "\n\n" + note
    hub.broadcast({"type": "typing", "agent": name, "room": m["room"]})  # viewer shows "thinking…"
    empty = []
    try:
        text = (await hub.ask(name, agent["model"], "\n\n".join(parts), system, hub.s.ask_max_tokens,
                              raw=True, empty_notice=empty.append)).strip()
    finally:
        hub.broadcast({"type": "typing", "agent": name, "room": m["room"], "done": True})
    text = _own_reply(text, name)
    if FILLER_RE.match(text):
        text = ""
    if not text:
        if not m["human"]:
            _gave_up[(name, m["id"])] = m["created"]
            return  # don't answer another agent with noise; they can ask again
        text = f"(empty answer: {empty[0]})" if empty else f"({agent['model']} returned nothing; try again or rephrase)"
    hub.post(name, m["room"], text, reply_to=m["id"])
    st.x("UPDATE agents SET last_seen=? WHERE name=?", (time.time(), name))


def _note(hub: Hub, agent, m, why: str) -> None:
    """Tells a person their message to an auto agent wasn't answered (once). Can't raise: a rate limit or a
    paused room just means no note."""
    name = agent["name"]
    try:
        if hub.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (m["id"], name)):
            return  # already answered or noted
        hub.post(name, m["room"], f"({name} didn't answer: {why}; ask again)", reply_to=m["id"])
    except Exception as e:
        log.warning("%s: no note for #%s: %s", name, m["id"], e)


def _state(hub: Hub) -> dict:
    """Per hub: the task answering for each agent, and the messages waiting for it."""
    return hub.__dict__.setdefault("_autoreply", {"tasks": {}, "queue": {}, "answering": {}})


def read_new(hub: Hub, agent) -> list:
    """The agent's new messages that are worth answering. Cheap and synchronous; the cursor moves here.
    Staleness is decided now, once: a message that merely waits in the agent's queue behind a slow
    answer must not count as stale later."""
    name, st = agent["name"], hub.store
    had = set(st.joined_rooms(name))  # rooms it was already serving, before this read adds the rest
    _header, rows, _more = hub.read_rows(name, None, False)
    if hub.worker_state(agent) == "offline":  # its model's machine is off: the hub already said so
        return []
    cutoff, out = time.time() - STALE_AFTER, []
    for m in rows:
        if f",{name}," not in (m["recipients"] or ""):
            continue
        if m["created"] < cutoff:
            # Old because the hub was down or busy: say so. Not when the agent is new to auto or to the room
            # (no earlier room cursor, or it never answered anything before this message): that's backlog.
            if m["human"] and m["room"] in had and agent["last_seen"] and m["created"] > agent["last_seen"]:
                _note(hub, agent, m, "it was too old by the time I looked")
            continue
        out.append(m)
    return out


async def serve_message(hub: Hub, agent, m) -> None:
    name = agent["name"]
    if (name, m["id"]) in _gave_up:
        return
    if hub.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (m["id"], name)):
        return  # already answered, e.g. with an "offline" note
    if hub.duplicate_of(m):
        return  # a double send: the first copy is answered
    try:
        await answer(hub, agent, m)
    except HubError as e:  # gateway down, budget used: say so once, don't retry forever
        log.warning("%s could not answer #%s: %s", name, m["id"], e)
        try:
            hub.post(name, m["room"], f"({agent['model']} didn't answer: {str(e)[:160]})", reply_to=m["id"])
        except HubError:
            pass  # room paused / rate limit: nothing more to do
    except asyncio.CancelledError:
        raise
    except Exception as e:  # a bug or an odd gateway reply: the person should see something, the queue goes on
        log.exception("%s failed on #%s", name, m["id"])
        if m["human"]:
            _note(hub, agent, m, f"unexpected error ({type(e).__name__})")


async def _drain(hub: Hub, name: str) -> None:
    """Answers one agent's queue, one message at a time. One task per agent, so a slow model only holds
    up its own agent."""
    state = _state(hub)
    try:
        while state["queue"].get(name):
            m = state["queue"][name].pop(0)
            agent = hub.store.agent(name)  # fresh: it may have been edited or deleted while waiting
            if not agent or not agent["model"]:
                state["queue"].pop(name, None)
                return
            state["answering"][name] = m["id"]
            try:
                await serve_message(hub, agent, m)
            finally:
                state["answering"].pop(name, None)
    except asyncio.CancelledError:
        raise
    except Exception:  # serve_message handles its own; this is the last net so the task never dies silently
        log.exception("auto agent %s: task failed", name)
    finally:
        if state["tasks"].get(name) is asyncio.current_task():
            del state["tasks"][name]


async def autoreply_once(hub: Hub, wait: bool = True) -> None:
    """One pass: reads every auto agent's new messages and hands them to that agent's task. wait=False
    (the loop) returns at once; the tasks go on in the background and the next pass can queue more."""
    cutoff = time.time() - STALE_AFTER  # older messages are skipped anyway: forget them
    for k in [k for k, created in _gave_up.items() if created < cutoff]:
        del _gave_up[k]
    await hub.refresh_model_hosts()
    state = _state(hub)
    for a in hub.store.auto_agents():
        name = a["name"]
        rows = read_new(hub, a)
        if hub.worker_state(a) != "offline":
            # The cursor can outlive the queue after a restart. Recover only fresh work.
            rows += [m for m in hub.unanswered(name) if m["created"] >= cutoff]
        pending = {m["id"] for m in state["queue"].get(name, [])}
        pending.add(state["answering"].get(name))
        for m in rows:
            if m["id"] in pending or m["id"] in hub.routing or (name, m["id"]) in _gave_up:
                continue
            state["queue"].setdefault(name, []).append(m)
            pending.add(m["id"])
        if state["queue"].get(name) and name not in state["tasks"]:
            state["tasks"][name] = asyncio.create_task(_drain(hub, name))
    if wait and state["tasks"]:
        await asyncio.gather(*state["tasks"].values(), return_exceptions=True)


async def _stop_tasks(hub: Hub) -> None:
    tasks = list(_state(hub)["tasks"].values())
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def autoreply_loop(hub: Hub) -> None:
    log.info("auto agents on")
    try:
        while True:
            woke = hub._event  # grab before the pass so a message posted mid-pass isn't missed
            try:
                await autoreply_once(hub, wait=False)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("auto-reply pass failed: %s", e)
            try:
                await asyncio.wait_for(woke.wait(), 30)
            except asyncio.TimeoutError:
                pass
    finally:
        await _stop_tasks(hub)  # shutdown: don't leave answers running
