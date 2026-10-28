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
          "Prefer agents marked auto (they answer right away). Reply with only agent names, "
          "comma-separated, at most 2, or the word none if no agent should answer.")


async def route(hub: Hub, room: str, text: str, timeout: float = 20.0) -> list[str]:
    """Returns agent names to address; [] when routing is off, fails, or picks nobody."""
    st, s = hub.store, hub.s
    if not s.router_model or not hub.llm.enabled:
        return []
    cutoff = time.time() - 600
    agents = [a for a in st.agents() if a["kind"] == "agent"]
    if not agents:
        return []
    roster = "\n".join(
        f"- {a['name']}: {a['description'] or 'no description'}"
        f" ({'auto' if a['model'] else 'manual'}{', active' if (a['last_seen'] or 0) > cutoff else ''})"
        for a in agents)
    user = (f"Agents:\n{roster}\n\nRoom #{room} summary: {one_line(hub.room_summary(room), 400)}\n\n"
            f"Message:\n{text[:2000]}")
    try:
        answer, tokens = await asyncio.wait_for(hub.llm.chat(s.router_model, [
            {"role": "system", "content": PROMPT}, {"role": "user", "content": user}], 40, user=ROUTER), timeout)
    except Exception as e:  # gateway down, model missing, timeout: fall back to room-wide
        log.warning("routing failed: %s", e)
        return []
    st.add_usage(ROUTER, ask=tokens)
    names = {a["name"] for a in agents}
    picked: list[str] = []
    for word in re.findall(r"[a-z0-9][a-z0-9_.-]*", (answer or "").lower()):
        if word in names and word not in picked:
            picked.append(word)
    return picked[:2]
