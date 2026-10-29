"""Addresses a human message that names nobody: a cheap model picks which agent(s)
should handle it, so the message lands in someone's `read` instead of nowhere."""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .hub import Hub, one_line

log = logging.getLogger("bunch.router")

ROUTER = "_router"
PROMPT = ("You route chat messages from a human to AI agents. Pick the agent(s) best suited to answer. "
          "Cheapest first: prefer agents marked auto (local models, answer right away). Pick a subscription "
          "agent only when the message needs a stronger model (hard reasoning, real code work) or no auto agent "
          "fits. Reply with only agent names, comma-separated, at most 2, or the word none if no agent should "
          "answer.")
LABELS = {"auto": "auto, cheap", "online": "subscription", "busy": "subscription, busy"}
FOLLOW_UP_WORDS = 3   # "try again", "go on", "thanks": too short for the model to route on its own
FOLLOW_UP_WINDOW = 1800


async def route(hub: Hub, room: str, text: str, timeout: float = 20.0) -> list[str]:
    """Returns agent names to address; [] when routing is off, fails, or picks nobody."""
    st, s = hub.store, hub.s
    if not s.router_model or not hub.llm.enabled:
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
    if len(text.split()) <= FOLLOW_UP_WORDS:  # a follow-up goes to whoever answered last here, not to a new pick
        last = st.one("SELECT sender FROM messages WHERE room=? AND human=0 AND created>? ORDER BY id DESC LIMIT 1",
                      (room, time.time() - FOLLOW_UP_WINDOW))
        if last and last["sender"] in names:
            return [last["sender"]]
    first = st.setting("first_responder")
    if first in names:  # the admin picked who takes un-@'d messages: no need to ask a model
        return [first]
    roster = "\n".join(f"- {a['name']}: {a['description'] or 'no description'} ({label})" for a, label in agents)
    user = (f"Agents:\n{roster}\n\nRoom #{room} summary: {one_line(hub.room_summary(room), 400)}\n\n"
            f"Message:\n{text[:2000]}")
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
