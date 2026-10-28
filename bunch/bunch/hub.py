"""The hub's actual behaviour. Every agent-facing tool lives here and returns
compact plain text, because whatever we return lands in an LLM's context window.
MCP and the REST API are just two doors into these same functions."""
from __future__ import annotations

import asyncio
import re
import time

from .config import Settings
from .llm import LLM, LLMError
from .store import Store, valid_name

MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_.-]{0,31})")


class HubError(Exception):
    """A user-facing error. Message is short on purpose (it costs tokens)."""


def est_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def fmt_size(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _recips(m) -> list[str]:
    return [r for r in (m["recipients"] or "").strip(",").split(",") if r]


class Hub:
    def __init__(self, store: Store, settings: Settings, llm: LLM):
        self.store = store
        self.s = settings
        self.llm = llm
        self.listeners: set[asyncio.Queue] = set()  # web viewer sockets
        self._event = asyncio.Event()                # wakes long-polling readers

    # ---------------------------------------------------------------- events
    def _notify(self, mid: int) -> None:
        msg = self.message_json(self.store.message(mid))
        for q in list(self.listeners):
            try:
                q.put_nowait({"type": "message", "message": msg})
            except asyncio.QueueFull:
                pass
        self._event.set()
        self._event = asyncio.Event()

    async def wait_for_new(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    def message_json(m) -> dict:
        return {
            "id": m["id"], "room": m["room"], "sender": m["sender"], "human": bool(m["human"]),
            "to": _recips(m), "summary": m["summary"], "body": m["body"],
            "reply_to": m["reply_to"], "created": m["created"],
        }

    def _charge_ctx(self, agent: str, text: str) -> str:
        self.store.add_usage(agent, ctx=est_tokens(text))
        return text

    # ------------------------------------------------------------------ post
    def post(self, sender: str, room: str, text: str, to=None, summary: str | None = None,
             reply_to: int | None = None, human: bool = False) -> str:
        room = (room or "").strip().lower().lstrip("#")
        if not valid_name(room):
            raise HubError("bad room name (lowercase a-z 0-9 _ . -)")
        text = (text or "").strip()
        if not text:
            raise HubError("empty text")
        if len(text) > self.s.max_body:
            raise HubError(f"text too long ({len(text)} > {self.s.max_body} chars)")

        if not human:
            self._check_rate(sender)
            self._check_chain(room)

        # Recipients: explicit `to` + @mentions of known names; replies default to the parent's author.
        known = self.store.agent_names()
        if isinstance(to, str):
            to = [t for t in re.split(r"[,\s]+", to) if t]
        recips: list[str] = []
        for name in list(to or []) + MENTION_RE.findall(text):
            name = name.lower().lstrip("@").rstrip(".")  # "@coder." ends a sentence
            if name in known and name != sender and name not in recips:
                recips.append(name)
        if reply_to is not None:
            parent = self.store.message(int(reply_to))
            if not parent:
                raise HubError(f"no message #{reply_to}")
            if not recips and parent["sender"] != sender:
                recips = [parent["sender"]]

        # Short text is its own summary. Long text is stored as a body; the summary is what readers see.
        auto = False
        if summary:
            summ, body = one_line(summary, self.s.summary_max), text
        elif len(text) <= self.s.summary_max and "\n" not in text:
            summ, body = text, None
        else:  # keep formatting in the body; summarizer may later improve a truncated summary
            summ, body = one_line(text, self.s.summary_max), text
            auto = len(text) > self.s.summary_max

        self.store.ensure_room(room)
        if self.store.cursor(sender, room) is None:          # posting joins you, skipping old backlog
            self.store.set_cursor(sender, room, self.store.room_max_id(room))
        mid = self.store.add_message(room, sender, human, recips, summ, body, reply_to, auto)
        self._notify(mid)
        return f"ok #{mid}"

    def _check_rate(self, sender: str) -> None:
        if self.s.posts_per_min <= 0:
            return
        row = self.store.one("SELECT COUNT(*) AS n FROM messages WHERE sender=? AND created>?",
                             (sender, time.time() - 60))
        if row["n"] >= self.s.posts_per_min:
            raise HubError("rate limit: too many posts this minute")

    def _check_chain(self, room: str) -> None:
        """Loop breaker: after max_chain agent-only messages in a row, a human has to speak."""
        n = self.s.max_chain
        if n <= 0:
            return
        rows = self.store.q("SELECT human FROM messages WHERE room=? ORDER BY id DESC LIMIT ?", (room, n))
        if len(rows) >= n and not any(r["human"] for r in rows):
            raise HubError(f"paused: {n} agent messages in a row in '{room}'; waiting for a human")

    # ------------------------------------------------------------------ read
    def read_rows(self, agent: str, room: str | None = None, everything: bool = False):
        """Returns (header_lines, rows, more). Advances the agent's cursors."""
        st, limit = self.store, self.s.read_limit
        me_like = f",{agent},"  # exact match (LIKE would treat _ as a wildcard)
        header: list[str] = []

        if room:
            room = room.strip().lower().lstrip("#")
            if not st.room(room):
                raise HubError(f"no room '{room}'")
            cur = st.cursor(agent, room)
            relevant = "1=1" if everything else "(recipients='' OR instr(recipients, ?) > 0)"
            args = () if everything else (me_like,)
            if cur is None:  # first visit: summary + short tail instead of the whole backlog
                r = st.room(room)
                if r["summary"]:
                    header.append(f"joined {room}. summary: {self.room_summary(room)}")
                else:
                    total = st.one("SELECT COUNT(*) AS n FROM messages WHERE room=?", (room,))["n"]
                    header.append(f"joined {room} ({total} msgs; latest below)")
                rows = st.q(f"SELECT * FROM messages WHERE room=? AND sender!=? AND {relevant} "
                            f"ORDER BY id DESC LIMIT ?", (room, agent, *args, self.s.join_tail))[::-1]
                st.set_cursor(agent, room, st.room_max_id(room))
                return header, rows, False
            rows = st.q(f"SELECT * FROM messages WHERE room=? AND id>? AND sender!=? AND {relevant} "
                        f"ORDER BY id LIMIT ?", (room, cur, agent, *args, limit + 1))
            more = len(rows) > limit
            rows = rows[:limit]
            st.set_cursor(agent, room, rows[-1]["id"] if more else st.room_max_id(room))
            return header, rows, more

        # All rooms: joined rooms (room-wide + addressed to me) and anywhere I'm @mentioned.
        joined_clause = "1=1" if everything else "(m.recipients='' OR instr(m.recipients, ?) > 0)"
        args = () if everything else (me_like,)
        rows = st.q(
            "SELECT m.* FROM messages m LEFT JOIN cursors c ON c.agent=? AND c.room=m.room "
            "WHERE m.sender!=? AND m.id>COALESCE(c.last_id,0) AND ("
            f"  (c.last_id IS NOT NULL AND {joined_clause}) OR instr(m.recipients, ?) > 0"
            ") ORDER BY m.id LIMIT ?",
            (agent, agent, *args, me_like, limit + 1),
        )
        more = len(rows) > limit
        rows = rows[:limit]
        if more:
            last = {}
            for r in rows:
                last[r["room"]] = r["id"]
            for rm, mid in last.items():
                st.set_cursor(agent, rm, mid)
        else:
            touched = set(st.joined_rooms(agent)) | {r["room"] for r in rows}
            for rm in touched:
                st.set_cursor(agent, rm, st.room_max_id(rm))
        return header, rows, more

    def fmt_line(self, m, agent: str, show_room: bool) -> str:
        s = f"#{m['id']}"
        if show_room:
            s += f" [{m['room']}]"
        s += f" {m['sender']}"
        rec = _recips(m)
        if rec:
            s += ">" + ",".join("you" if r == agent else r for r in rec)
        if m["reply_to"]:
            s += f" re#{m['reply_to']}"
        s += f": {m['summary']}"
        if m["body"]:
            s += f" (+{fmt_size(len(m['body']))})"
        return s

    def read(self, agent: str, room: str | None = None, everything: bool = False) -> str:
        header, rows, more = self.read_rows(agent, room, everything)
        lines = header + [self.fmt_line(m, agent, show_room=not room) for m in rows]
        if more:
            lines.append("(more: read again)")
        if not rows and not header:
            lines.append("no new messages")
        return self._charge_ctx(agent, "\n".join(lines))

    def room_summary(self, room: str) -> str:
        r = self.store.room(room)
        if r and r["summary"]:
            newer = self.store.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>?",
                                   (room, r["summary_upto"]))["n"]
            return r["summary"] + (f" (+{newer} newer)" if newer else "")
        rows = self.store.q("SELECT sender, summary FROM messages WHERE room=? ORDER BY id DESC LIMIT 5", (room,))
        if not rows:
            return "(empty)"
        return "recent: " + " | ".join(f"{x['sender']}: {one_line(x['summary'], 70)}" for x in rows[::-1])

    # ------------------------------------------------------------------- get
    def get(self, agent: str, mid: int, offset: int = 0) -> str:
        m = self.store.message(int(mid))
        if not m:
            raise HubError(f"no message #{mid}")
        text = m["body"] or m["summary"]
        offset = max(0, int(offset or 0))
        chunk = text[offset: offset + self.s.get_max]
        head = f"#{m['id']} {m['sender']} in {m['room']}"
        if m["reply_to"]:
            head += f" re#{m['reply_to']}"
        if len(text) > self.s.get_max or offset:
            head += f" [chars {offset}-{offset + len(chunk)} of {len(text)}]"
        out = head + "\n" + chunk
        if offset + len(chunk) < len(text):
            out += f"\n(more: get id={m['id']} offset={offset + len(chunk)})"
        return self._charge_ctx(agent, out)

    # ----------------------------------------------------------------- rooms
    def rooms(self, agent: str) -> str:
        st = self.store
        joined = st.joined_rooms(agent)
        lines = []
        for r in st.rooms()[:50]:
            name = r["name"]
            mine = st.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>? AND sender!=? "
                          "AND instr(recipients, ?) > 0", (name, joined.get(name, 0), agent, f",{agent},"))["n"]
            if name in joined:
                new = st.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>? AND sender!=? "
                             "AND (recipients='' OR instr(recipients, ?) > 0)",
                             (name, joined[name], agent, f",{agent},"))["n"]
                status = f"{new} new" + (f", {mine} for you" if mine else "")
            else:
                status = "not joined" + (f", {mine} for you" if mine else "")
            lines.append(f"{name} ({status}): {one_line(self.room_summary(name), 100)}")
        if not lines:
            lines.append("no rooms yet (post to create one)")
        cutoff = time.time() - 600
        names = [a["name"] + ("*" if (a["last_seen"] or 0) > cutoff else "")
                 for a in st.agents() if a["kind"] == "agent" and a["name"] != agent]
        lines.append("agents: " + (", ".join(names) or "none") + "  (*=active 10m)")
        return self._charge_ctx(agent, "\n".join(lines))

    # ------------------------------------------------------------------- ask
    def budget(self, agent: str) -> int:
        row = self.store.agent(agent)
        if row and row["daily_tokens"] is not None:
            return int(row["daily_tokens"])
        return self.s.daily_tokens

    async def ask(self, agent: str, model: str, prompt: str | None = None, system: str | None = None,
                  max_tokens: int | None = None, room: str | None = None) -> str:
        if not self.llm.enabled:
            raise HubError("no model gateway configured (LITELLM_URL)")
        model = (model or "").strip()
        try:
            if model in ("", "?"):
                return self._charge_ctx(agent, "models: " + ", ".join(await self.llm.models()))
            if not prompt:
                raise HubError("prompt required")
            budget = self.budget(agent)
            used = self.store.usage_today(agent)["ask"]
            if budget and used >= budget:
                raise HubError(f"daily token budget used ({used}/{budget}); resets 00:00 UTC")
            mt = max(1, min(int(max_tokens or self.s.ask_max_tokens), self.s.ask_max_tokens))
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            text, tokens = await self.llm.chat(model, msgs, mt, user=agent)
        except LLMError as e:
            raise HubError(str(e)) from e
        self.store.add_usage(agent, ask=tokens)
        used += tokens
        note = f"\n[budget {used}/{budget} today]" if budget and used > 0.8 * budget else ""
        if room:  # put the answer in a room for visibility; hand back only the short line
            res = self.post(agent, room, text or "(empty answer)")
            mid = int(res.split("#")[1])
            return self._charge_ctx(agent, f"posted {self.fmt_line(self.store.message(mid), agent, True)}{note}")
        return self._charge_ctx(agent, (text or "(empty answer)") + note)
