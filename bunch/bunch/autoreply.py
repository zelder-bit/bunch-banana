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

from .hub import ECHO_SIMILARITY, Hub, HubError, _shingles
from .store import text_of

log = logging.getLogger("bunch.autoreply")

DEFAULT_SYSTEM = ("You are {name}, an AI agent in a shared chat room with other agents and humans. "
                  "Answer the message addressed to you. Be brief and concrete. "
                  "Recent messages are listed: build on what was already said instead of repeating it. "
                  "Only @mention another agent when you need it to act now; to point a human at an agent, "
                  "write its name without @. Write only your own reply, as plain text: never write lines "
                  "for other speakers or in the 'sender>to re#id:' format of the list.")
MAX_INPUT_CHARS = 12_000
MAX_SAVES = 5  # files written from one reply
SAVE_HELP = ("To save a file in the room's shared folder, put a line `save: <relative/path>` directly above a "
             "fenced code block holding the whole file. You cannot run anything.")
NO_FOLDER_HELP = ("You cannot upload, save or run anything here: put code in your reply, and never claim "
                  "you saved, uploaded or ran something.")
# "save: path" (maybe in backticks/bold) above a fenced block (maybe a blank line between, maybe a language tag)
SAVE_RE = re.compile(r"^[ \t]*[*_]*save[*_]*:[*_]*[ \t]*[`*_]*([^\s`*]+?)[`*_]*[ \t]*\n(?:[ \t]*\n)?[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
                     re.I | re.M | re.S)
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


SUMMARY_RE = re.compile(r"^[\s#*_>-]*summary of\b", re.I)
MIN_ECHO_WORDS = 15  # shorter replies may quote context freely


def _is_echo(raw: str, name: str, others: set[str], context: list[str]) -> bool:
    """True when a model went on with the prompt instead of answering it: it addresses itself, continues the
    room summary, copies the summary or recent messages, or writes lines for other speakers. Conservative:
    a real answer that quotes a little context is not an echo."""
    text = _own_reply(raw, name)
    if re.match(rf"^[\W_]*(?:@{re.escape(name)}\b|(?:hey|hi|hello)[\s,]+@?{re.escape(name)}\b)", text, re.I):
        return True
    if SUMMARY_RE.match(text):
        return True
    words = len(re.findall(r"\w+", text))
    if words >= MIN_ECHO_WORDS:  # share of the reply's word trigrams found in the prompt context
        mine = _shingles(text)
        theirs = set().union(*(_shingles(c) for c in context if c))
        if mine and len(mine & theirs) / len(mine) >= ECHO_SIMILARITY:
            return True
    lines, in_code, other = [ln for ln in raw.split("\n") if ln.strip()], False, 0
    first = SPEAKER_RE.match(lines[0]) if lines else None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif in_code or (i == 0 and first and first.group(1).lower() == name.lower()):
            continue
        else:
            sp = SPEAKER_RE.match(line) or re.match(r"^\s*[*@]*([\w.-]+)[*]*:\s", line)
            if sp and sp.group(1).lower() != name.lower() and (SPEAKER_RE.match(line) or sp.group(1) in others):
                other += 1
    return other >= 1 and (not text or (other >= 2 and other * 2 >= len(lines)))


async def _save_files(hub: Hub, name: str, room: str, text: str) -> str:
    """Writes the `save: path` + code block pairs of a reply to the room folder; returns the reply with one
    status line per file. A failed write is only reported: the reply is posted anyway."""
    notes = []
    for mt in list(SAVE_RE.finditer(text))[:MAX_SAVES]:
        path = mt.group(1)
        try:
            await hub.files_write(name, room, path, mt.group(2).encode())
            notes.append(f"(saved {path} to the room folder)")
        except HubError as e:
            notes.append(f"(could not save {path}: {e})")
        except Exception as e:  # never lose the reply over a file
            log.warning("%s: save %s failed: %s", name, path, e)
            notes.append(f"(could not save {path}: {type(e).__name__})")
    return text + "\n\n" + "\n".join(notes) if notes else text


async def answer(hub: Hub, agent, m) -> None:
    name = agent["name"]
    body = text_of(m)
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
                         f"{text_of(parent)[:MAX_INPUT_CHARS // 3]}")
    parts.append(f"Message #{m['id']} from {m['sender']}:\n{body[:MAX_INPUT_CHARS]}")
    system = agent["system"] or DEFAULT_SYSTEM.format(name=name)
    note = hub.room_note(m["room"])
    has_folder = bool(st.folder(m["room"]))
    system += "\n\n" + (SAVE_HELP if has_folder else NO_FOLDER_HELP)
    if note:  # bot mode: keep handing work on instead of waiting for the person
        system += "\n\n" + note
    hub.broadcast({"type": "typing", "agent": name, "room": m["room"]})  # viewer shows "thinking…"
    empty: list[str] = []
    context = [parts[0], recent or ""]
    others = st.agent_names() - {name}

    async def ask(prompt: str) -> tuple[str, bool]:
        raw = (await hub.ask(name, agent["model"], prompt, system, hub.s.ask_max_tokens,
                             raw=True, empty_notice=empty.append)).strip()
        return _own_reply(raw, name), _is_echo(raw, name, others, context)
    try:
        prompt = "\n\n".join(parts)
        text, echo = await ask(prompt)
        if echo:  # it went on with the prompt: one more try, told what to do
            log.info("%s: echo for #%s, retrying", name, m["id"])
            text, echo = await ask(f"{prompt}\n\nAnswer only message #{m['id']}, in your own words, as {name}. "
                                   "Do not summarize the room, do not write for other people, do not address yourself.")
    finally:
        hub.broadcast({"type": "typing", "agent": name, "room": m["room"], "done": True})
    if FILLER_RE.match(text) or echo:
        text = ""
    if not text:
        if not m["human"]:
            _gave_up[(name, m["id"])] = m["created"]
            return  # don't answer another agent with noise; they can ask again
        text = (f"({agent['model']} gave no usable answer; ask again or rephrase)" if echo else
                f"(empty answer: {empty[0]})" if empty else f"({agent['model']} returned nothing; try again or rephrase)")
    if has_folder:
        text = await _save_files(hub, name, m["room"], text)
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
            except TimeoutError:
                pass
    finally:
        await _stop_tasks(hub)  # shutdown: don't leave answers running
