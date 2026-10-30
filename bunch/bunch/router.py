"""Addresses a human message that names nobody: a cheap model picks which agent(s)
should handle it, so the message lands in someone's `read` instead of nowhere."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .hub import MENTION_RE, Hub, one_line

log = logging.getLogger("bunch.router")

ROUTER = "_router"
PROMPT = ("You route chat messages from a human to AI agents. Pick the agent(s) best suited to answer. "
          "Cheapest first: prefer agents marked auto (local models, answer right away). Pick a subscription "
          "agent only when the message needs a stronger model (hard reasoning, real code work) or no auto agent "
          "fits. Usually pick exactly one: two agents answering the same question at once just repeat each "
          "other. Pick two only when the message asks for two different specialties. If the message continues "
          "a conversation with an agent (refers back to it, asks for more detail), pick that agent. Reply with only agent names, "
          "comma-separated, or the word none if no agent should answer.")
LABELS = {"auto": "auto, cheap", "online": "subscription", "busy": "subscription, busy"}
FOLLOW_UP_WORDS = 3   # "try again", "go on", "thanks": too short for the model to route on its own
FOLLOW_UP_WINDOW = 1800
# "go more in depth with this", "can you expand on that": longer, but still a follow-up to the last answer
BACK_REF_RE = re.compile(r"\b(?:this|that|it|those|these|more|again|above|expand|elaborate|in depth|continue)\b", re.I)


async def route_agent_post(hub: Hub, sender: str, room: str, text: str, to, reply_to) -> list[str] | None:
    """Bot mode: a new message from an agent that names no agent (say a goal from an agent outside the
    run) is routed like a person's, so it starts the run instead of waking nobody. Returns the `to` to post with."""
    room = (room or "").strip().lower().lstrip("#")
    known = hub.store.agent_names()
    if to or reply_to not in (None, "") or not hub.bot(room) or \
            any(n.rstrip(".") in known for n in MENTION_RE.findall((text or "").lower())):
        return to
    return [n for n in await route(hub, room, text, sender=sender) if n != sender] or to


async def route(hub: Hub, room: str, text: str, timeout: float = 20.0, sender: str | None = None) -> list[str]:
    """Returns agent names to address; [] when nobody is picked. The first responder needs no model, so it
    takes un-@'d messages even with routing off; the follow-up rules and the model pick need routing on."""
    st, s = hub.store, hub.s
    routing = bool(s.router_model and hub.llm.enabled)
    first = st.setting("first_responder")
    if not routing and not first:
        return []
    await hub.refresh_model_hosts()  # a gateway model whose machine is off is offline too
    cutoff = time.time() - 600
    agents = []
    for a in st.agents():
        if a["kind"] != "agent":
            continue
        state = hub.worker_state(a)
        if state in ("cooling", "offline"):
            continue  # out of quota or not running: picking it would strand the message
        if state == "manual":
            label = "manual, answers only when its person checks" + (", active" if (a["last_seen"] or 0) > cutoff else "")
        else:
            cfg = hub.worker_config(a)
            label = LABELS[state] + ("" if state == "auto" else f" {cfg['cli']} on {cfg['machine']}")
        agents.append((a, label))
    if not agents:
        return []
    names = {a["name"] for a, _ in agents}
    # A follow-up goes to whoever answered last here, not to a new pick: any very short message, or one
    # that refers back when the last agent message answered this same person.
    last = st.one("SELECT sender, reply_to FROM messages WHERE room=? AND human=0 AND created>? ORDER BY id DESC LIMIT 1",
                  (room, time.time() - FOLLOW_UP_WINDOW)) if routing else None
    if last and last["sender"] in names:
        if len(text.split()) <= FOLLOW_UP_WORDS:
            return [last["sender"]]
        asked = st.message(last["reply_to"]) if last["reply_to"] else None
        if sender and asked and asked["sender"] == sender and BACK_REF_RE.search(text):
            return [last["sender"]]
    if first in names:  # the admin picked who takes un-@'d messages: no need to ask a model
        return [first]
    if not routing:  # first responder is offline or cooling: the message goes to the whole room
        return []
    roster = "\n".join(f"- {a['name']}: {a['description'] or 'no description'} ({label})" for a, label in agents)
    recent = hub.recent_lines(room, 0, 6)
    user = (f"Agents:\n{roster}\n\nRoom #{room} summary: {one_line(hub.room_summary(room), 400)}\n\n"
            + (f"Recent messages:\n{recent}\n\n" if recent else "") + f"Message:\n{text[:2000]}")
    try:
        answer, tokens = await asyncio.wait_for(hub.llm.chat(s.router_model, [
            {"role": "system", "content": PROMPT}, {"role": "user", "content": user}], 40, user=ROUTER), timeout)
    except Exception as e:  # gateway down, model missing, timeout: fall back to room-wide
        log.warning("routing failed: %s", e)
        return []
    st.add_usage(ROUTER, ask=tokens)
    picked: list[str] = []
    for word in re.findall(r"[a-z0-9][a-z0-9_.-]*", (answer or "").lower()):
        if word in names and word not in picked:
            picked.append(word)
    states = {a["name"]: hub.worker_state(a) for a, _ in agents}
    autos = [n for n in picked if states[n] == "auto"]
    picked = [n for n in picked if states[n] != "auto" or n == autos[0]]  # two local models saying the same thing is noise
    return picked[:2]
