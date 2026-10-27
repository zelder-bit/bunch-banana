"""Background job: keeps a short rolling summary per room and writes real one-line
summaries for long messages, using a cheap (ideally local) model."""
from __future__ import annotations

import asyncio
import logging

from .hub import Hub, one_line

log = logging.getLogger("bunch.summarizer")

MSG_PROMPT = ("Summarize the message in one line of at most 20 words. "
              "Keep names, numbers and decisions. Output only the line.")
ROOM_PROMPT = ("You maintain the running summary of a chat room between AI agents and humans. "
               "Merge the new messages into the previous summary. At most 120 words. Keep: goals, "
               "decisions, open questions, who is doing what, message ids worth reading (#id). "
               "Drop chit-chat. Output only the summary.")
SUMMARIZER = "_summarizer"


async def summarize_once(hub: Hub) -> int:
    """One pass. Returns how many summaries were written."""
    st, s, llm = hub.store, hub.s, hub.llm
    done = 0

    for m in st.q("SELECT id, body FROM messages WHERE auto_sum=1 ORDER BY id LIMIT 10"):
        text, tok = await llm.chat(s.summary_model, [
            {"role": "system", "content": MSG_PROMPT},
            {"role": "user", "content": (m["body"] or "")[:8000]},
        ], 80, user=SUMMARIZER)
        st.add_usage(SUMMARIZER, ask=tok)
        line = one_line(text, s.summary_max)
        if line:
            st.x("UPDATE messages SET summary=?, auto_sum=0 WHERE id=?", (line, m["id"]))
        else:
            st.x("UPDATE messages SET auto_sum=0 WHERE id=?", (m["id"],))
        done += 1

    for r in st.rooms():
        new = st.q("SELECT id, sender, summary FROM messages WHERE room=? AND id>? ORDER BY id LIMIT 200",
                   (r["name"], r["summary_upto"]))
        if len(new) < max(1, s.summary_every):
            continue
        convo = "\n".join(f"#{x['id']} {x['sender']}: {x['summary']}" for x in new)
        text, tok = await llm.chat(s.summary_model, [
            {"role": "system", "content": ROOM_PROMPT},
            {"role": "user", "content": f"Previous summary:\n{r['summary'] or '(none)'}\n\nNew messages:\n{convo}"},
        ], 300, user=SUMMARIZER)
        st.add_usage(SUMMARIZER, ask=tok)
        summary = " ".join((text or "").split())[:1200]
        if summary:
            st.x("UPDATE rooms SET summary=?, summary_upto=? WHERE name=?", (summary, new[-1]["id"], r["name"]))
            done += 1
    return done


async def summarizer_loop(hub: Hub, interval: float = 20.0) -> None:
    log.info("summarizer on (model=%s)", hub.s.summary_model)
    while True:
        try:
            await summarize_once(hub)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # gateway down, model missing, etc. Try again later.
            log.warning("summarizer pass failed: %s", e)
        await asyncio.sleep(interval)
