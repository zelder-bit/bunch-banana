"""The hub's actual behaviour. Every agent-facing tool lives here and returns
compact plain text, because whatever we return lands in an LLM's context window.
MCP and the REST API are just two doors into these same functions."""
from __future__ import annotations

import asyncio
import json
import posixpath
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from urllib.parse import urlsplit

from .config import Settings
from .llm import LLM, LLMError
from .store import HUB_SENDER, Store, text_of, valid_name

VIEWER_INLINE_BODY_CHARS = 2_000  # longer bodies are fetched when the viewer expands a message

WORKER_CLIS = ("claude", "codex", "agy")
CACHE_WEIGHT = 0.1  # a cached input token costs about a tenth of a fresh one (Anthropic, OpenAI, Google)
WORKER_POWERS = ("answer", "read", "full")  # no tools / read files in dir / edit + run in dir
WORKER_STATUSES = ("online", "busy", "cooling")
WORKER_OFFLINE_AFTER = 180  # a machine long-polls every ~50 s; silent this long = off or not running
RECENT_LINES = 8  # one-line summaries of the latest messages shown with every answer prompt
DUPLICATE_WINDOW = 600  # the same text from the same human this soon after is a double send
MAX_AGENT_HOPS = 2  # at most this many agent messages in a reply chain below a human message
REDELIVER_WINDOW = 900  # a job a worker fetched but never answered is offered again while it is this fresh
MAX_OFFERS = 2          # ...but only this many times in all, so a job that kills its worker can't loop
TURN_WAIT = 180  # agents addressed together answer in turn; stop waiting on a slow one after this long

# Bot mode (per room, admins switch it on): agents hand work to each other without a person, until one
# posts [done]. One baton per message, and a repeat or a two-agent back-and-forth stops the run.
DONE_RE = re.compile(r"^\s*\[done\]\s*$", re.I | re.M)
ECHO_SIMILARITY = 0.6  # word-trigram overlap with a recent message that counts as a repeat
ECHO_LOOKBACK = 8
PING_PONG = 6  # this many agent messages in a row from just two agents is a back-and-forth
BOT_NOTE = ("Bot mode is on in this room: you and the other agents work on the person's goal without "
            "waiting for them. Add something new each turn; don't restate what's in the recent messages. "
            "End with exactly one handoff that starts a line, like '@name, can you <next step>', or, when "
            "the goal is fully met, a line with just [done].")

FILE_REF = re.compile(r"!\[([^\]]*)\]\(file:(\d+)\)")  # an attached file, see /api/v1/machine/file
MENTION_RE = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9_.-]{0,31})")
# "@events, can you…" / "- **@legal**: please…": mentions that open a line or sentence and then ask.
# Also "@events - see above", "@events — two bugs", and a short greeting first: "hi @events, can you…".
_AT = r"\**@[a-z0-9][a-z0-9_.-]*\**"
_GREETING = r"(?:(?:hi|hey|hello|ok|okay|thanks|thank you)[\s,!]+)?"
ADDRESS_RE = re.compile(r"(?:^|[\n.!?])[\s>*_#-]*" + _GREETING + r"(" + _AT + r"(?:\s*(?:,|and|&)\s*" + _AT + r")*)"
                        r"\s*(?:[,:\u2013\u2014-]|(?:can|could|please|would|will)\b)")
# Bot mode also takes any line that starts with a mention as a handoff ("@bob what is\u2026"), as BOT_NOTE asks.
LINE_AT_RE = re.compile(r"^[\s>*_#-]*" + _GREETING + r"(" + _AT + r")(?=[\s,:;!?]|$)", re.M)


class HubError(Exception):
    """A user-facing error. Message is short on purpose (it costs tokens)."""


def est_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def fmt_size(n: int) -> str:
    return str(n) if n < 1000 else f"{n / 1000:.1f}k"


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _snippet(text: str, q: str, width: int = 160) -> str:
    """One line of text around the first match of q."""
    i = text.lower().find(q.lower())
    start = max(0, i - width // 3) if i >= 0 else 0
    piece = " ".join(text[start:start + width].split())
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


def post_id(result: str) -> int:
    """The message id in a post result ("ok #12", maybe followed by a hint)."""
    return int(re.search(r"#(\d+)", result)[1])  # type: ignore[index]  # only called on "ok #N" results


def mentions(text: str, name: str) -> bool:
    """True when text @mentions exactly this name (@gemini is not in "@gemini-local")."""
    return name in (n.rstrip(".") for n in MENTION_RE.findall((text or "").lower()))


def addresses(text: str, name: str) -> bool:
    """True when text asks this agent something ("@events, can you…"), not just names it
    ("check with @legal about…"): only that wakes an agent when another agent writes it."""
    return any(name in (n.rstrip(".") for n in MENTION_RE.findall(g))
               for g in ADDRESS_RE.findall((text or "").lower()))


_HANDOFF_LINE = re.compile(r"^[\s>*_#-]*(?:\**@[a-z0-9][a-z0-9_.-]*\**[\s,:]|\[done\]).*$", re.I | re.M)


def _shingles(text: str) -> set:
    """Word trigrams of text without its handoff lines ("@bb, can you…"), which every turn shares."""
    words = re.findall(r"\w+", _HANDOFF_LINE.sub("", (text or "").lower()))
    return {tuple(words[i:i + 3]) for i in range(len(words) - 2)} or {(w,) for w in words}


def _similar(a: str, b: str) -> float:
    """Word-trigram Jaccard similarity: 1.0 for the same text, near 0 for unrelated text."""
    sa, sb = _shingles(a), _shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def _recips(m) -> list[str]:
    return [r for r in (m["recipients"] or "").strip(",").split(",") if r]


class Hub:
    def __init__(self, store: Store, settings: Settings, llm: LLM):
        self.store = store
        self.s = settings
        self.llm = llm
        self._shingle_cache: OrderedDict = OrderedDict()  # bounded rooms and recent texts; edits change the cache key
        self.listeners: set[asyncio.Queue] = set()  # web viewer sockets
        self._event = asyncio.Event()                # wakes long-polling readers
        self._hosts: dict[str, str] = {}            # gateway model -> host, from LiteLLM
        self._hosts_at = 0.0
        self.routing: dict[int, tuple[str, float]] = {}  # message id -> (room, since): shown, recipients still being chosen
        self.offered: dict[tuple[str, int], int] = {}  # (worker agent, message id) -> times handed to its worker

    # ---------------------------------------------------------------- events
    def broadcast(self, event: dict) -> None:
        """Send an event to every open web viewer."""
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A viewer too slow to keep up would silently miss messages. Drop its backlog and tell its
                # socket to close instead: the viewer reconnects and catches up from the API.
                while not q.empty():
                    q.get_nowait()
                q.put_nowait({"type": "lagged"})

    def agents_changed(self, gone: str | None = None) -> None:
        """Every open viewer reloads its agent list (names, budgets, first responder, people).
        gone: an agent that was just deleted, so nothing stays queued for it."""
        if gone:
            for key in [k for k in self.offered if k[0] == gone]:
                del self.offered[key]
        self.broadcast({"type": "agents"})

    def _wake(self) -> None:
        """Wake current waiters and give future waiters a fresh event."""
        self._event.set()
        self._event = asyncio.Event()

    def _notify(self, mid: int) -> None:
        self.broadcast({"type": "message", "message": self.message_json(self.store.message(mid))})
        self._wake()

    async def wait_for_new(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            pass

    @staticmethod
    def message_json(m) -> dict:
        return {
            "id": m["id"], "room": m["room"], "sender": m["sender"], "human": bool(m["human"]),
            "to": _recips(m), "summary": m["summary"], "size": len(m["body"] or ""),
            "body": m["body"] if len(m["body"] or "") <= VIEWER_INLINE_BODY_CHARS else None,
            "reply_to": m["reply_to"], "created": m["created"],
        }

    def _charge_ctx(self, agent: str, text: str) -> str:
        self.store.add_usage(agent, ctx=est_tokens(text))
        return text

    # ------------------------------------------------------------------ post
    def post(self, sender: str, room: str, text: str, to=None, summary: str | None = None,
             reply_to: int | None = None, human: bool = False, routing: bool = False) -> str:
        """routing: the caller is still choosing who answers and will call finish_routing(). People see the
        message at once; agents don't read it, or anything after it in the room, until then."""
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
            summ, body = one_line(FILE_REF.sub(lambda m: f"[{m[1] or 'file'}]", text), self.s.summary_max), text
            auto = len(text) > self.s.summary_max

        self.store.ensure_room(room)
        if self.store.cursor(sender, room) is None:          # posting joins you, skipping old backlog
            self.store.set_cursor(sender, room, self.store.room_max_id(room))
        mid = self.store.add_message(room, sender, human, recips, summ, body, reply_to, auto)
        if routing and not recips:
            self.routing[mid] = (room, time.time())  # before anyone is woken
        self._notify(mid)
        if recips:
            self._unavailable_notices(mid)
        if not human and sender != HUB_SENDER and self.bot(room):
            self._bot_turn(mid)
        hint = "" if human or sender == HUB_SENDER else self._unasked_hint(sender, room, text)
        return f"ok #{mid}" + hint

    def _not_answered(self, agent: str, m, note: str) -> None:
        if self.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (m["id"], agent)):
            return
        # These replies close a job even when rate/chain limits prevent an ordinary post.
        mid = self.store.add_message(m["room"], agent, False, [m["sender"]], note,
                                     None, m["id"], False)
        self._notify(mid)

    def rejected_reply(self, agent: str, room: str, reply_to: int | None, why: str) -> None:
        try:
            if reply_to is None:
                return
            m = self.store.message(reply_to)
            room = (room or "").strip().lower().lstrip("#")
            if not m or room != m["room"] or agent not in _recips(m):
                return
            self.offered[(agent, reply_to)] = MAX_OFFERS  # fallback if storing the note fails
            why = "the room is paused" if why.startswith("paused:") else one_line(why, 180)
            self._not_answered(agent, m, f"({agent}'s answer was not posted: {why}; not answered)")
        except Exception:
            pass  # the note is a courtesy; keep the original rejection even if it fails

    def finish_routing(self, mid: int, to: list[str] | None) -> None:
        """Attaches the recipients chosen for a message posted with routing=True and lets agents read it.
        With nobody chosen it stays a message for the whole room."""
        self.routing.pop(mid, None)
        m = self.store.message(mid)
        if m:
            known = self.store.agent_names()
            recips = [n for n in dict.fromkeys(to or []) if n in known and n != m["sender"]]
            if recips:
                self.store.x("UPDATE messages SET recipients=? WHERE id=?", ("," + ",".join(recips) + ",", mid))
                self.broadcast({"type": "routed", "message": self.message_json(self.store.message(mid))})
                self._unavailable_notices(mid)
        self._wake()  # wake readers: the message can be read now

    def _routing_holds(self) -> dict[str, int]:
        """Room -> the first message there whose recipients are still being chosen. An entry older than a
        minute is dropped: its router task is gone, and the message stays one for the whole room."""
        hold: dict[str, int] = {}
        for mid, (room, since) in list(self.routing.items()):
            if time.time() - since > 60:
                del self.routing[mid]
            else:
                hold[room] = min(hold.get(room, mid), mid)
        return hold

    def _unasked_hint(self, sender: str, room: str, text: str) -> str:
        """An agent that names an auto or machine agent without asking it gets no answer from it, so say so
        in the post result (agents only see "ok #N" otherwise and think they asked). Agents that read for
        themselves (manual) still see the message in their read, so they get no hint."""
        baton = self._baton(text, sender) if self.bot(room) else None
        named: list[str] = []
        for n in MENTION_RE.findall(text.lower()):
            n = n.rstrip(".")
            row = self.store.agent(n)
            if n != sender and n not in named and row and row["kind"] == "agent" and \
                    self.worker_state(row) != "manual" and not addresses(text, n) and n != baton:
                named.append(n)
        if not named:
            return ""
        who = ", ".join(f"@{n}" for n in named)
        return (f" (named {who} but didn't ask, so {'it' if len(named) == 1 else 'they'} won't answer: "
                f"start a line with \"@{named[0]}, can you…\")")

    def _check_rate(self, sender: str) -> None:
        if self.s.posts_per_min <= 0:
            return
        row = self.store.one("SELECT COUNT(*) AS n FROM messages WHERE sender=? AND created>?",
                             (sender, time.time() - 60))
        if row["n"] >= self.s.posts_per_min:
            raise HubError("rate limit: too many posts this minute")

    def paused(self, room: str) -> bool:
        """Loop breaker: after max_chain agent-only messages in a row, a human has to speak.
        Not in bot mode, where the baton rules (see wakes) stop loops instead."""
        n = self.s.max_chain
        if n <= 0 or self.bot(room):
            return False
        # A failed answer neither advances the chain nor resets it.
        rows = self.store.q("SELECT human FROM messages WHERE room=? "
                            "AND (human=1 OR COALESCE(body, summary) NOT LIKE '(%; not answered)') "
                            "ORDER BY id DESC LIMIT ?", (room, n))
        return len(rows) >= n and not any(r["human"] for r in rows)

    def _check_chain(self, room: str) -> None:
        if self.paused(room):
            raise HubError(f"paused: {self.s.max_chain} agent messages in a row in '{room}'; waiting for a human")

    # ------------------------------------------------------------------ read
    def read_rows(self, agent: str, room: str | None = None, everything: bool = False):
        """Returns (header_lines, rows, more). Advances the agent's cursors."""
        st, limit = self.store, self.s.read_limit
        me_like = f",{agent},"  # exact match (LIKE would treat _ as a wildcard)
        header: list[str] = []
        # A message whose recipients are still being chosen (#76) is not read yet, and the cursor stops
        # before it, so whoever is picked finds it as new.
        hold = self._routing_holds()

        def upto(rm: str, mid: int) -> int:
            return min(mid, hold[rm] - 1) if rm in hold else mid

        def ready(found: list) -> list:
            return [r for r in found if r["id"] < hold.get(r["room"], r["id"] + 1)] if hold else found

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
                st.set_cursor(agent, room, upto(room, st.room_max_id(room)))
                return header, ready(rows), False
            rows = st.q(f"SELECT * FROM messages WHERE room=? AND id>? AND sender!=? AND {relevant} "
                        f"ORDER BY id LIMIT ?", (room, cur, agent, *args, limit + 1))
            rows = ready(rows)
            more = len(rows) > limit
            rows = rows[:limit]
            st.set_cursor(agent, room, rows[-1]["id"] if more else upto(room, st.room_max_id(room)))
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
        rows = ready(rows)
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
                st.set_cursor(agent, rm, upto(rm, st.room_max_id(rm)))
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

    def unanswered(self, agent: str) -> list:
        """Recent messages addressed to agent that it hasn't replied to, whether or not its cursor has
        passed them. The cursor moves when a worker fetches a job, before anything confirms it was
        handled; this is what lets the job be offered again after a crash, a lost response or a hub
        restart, and what keeps a message that is waiting for its turn."""
        if len(self.offered) > 5000:  # only recent messages matter; don't grow for ever
            self.offered.clear()
        return self.store.q(
            "SELECT m.* FROM messages m WHERE m.created>? AND m.sender!=? AND instr(m.recipients, ?) > 0 "
            "AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.reply_to=m.id AND r.sender=?) ORDER BY m.id",
            (time.time() - REDELIVER_WINDOW, agent, f",{agent},", agent))

    def read(self, agent: str, room: str | None = None, everything: bool = False) -> str:
        header, rows, more = self.read_rows(agent, room, everything)
        lines = header + [self.fmt_line(m, agent, show_room=not room) for m in rows]
        if more:
            lines.append("(more: read again)")
        if not rows and not header:
            lines.append("no new messages")
        return self._charge_ctx(agent, "\n".join(lines))

    def room_summary(self, room: str, fallback: bool = True) -> str:
        """The rolling summary. Without one yet, agents get the last few messages instead (fallback);
        the viewer passes fallback=False, since those messages are already on screen below it."""
        r = self.store.room(room)
        if r and r["summary"]:
            newer = self.store.one("SELECT COUNT(*) AS n FROM messages WHERE room=? AND id>?",
                                   (room, r["summary_upto"]))["n"]
            return r["summary"] + (f" (+{newer} newer)" if newer else "")
        if not fallback:
            return ""
        rows = self.store.q("SELECT sender, summary FROM messages WHERE room=? ORDER BY id DESC LIMIT 5", (room,))
        if not rows:
            return "(empty)"
        return "recent: " + " | ".join(f"{x['sender']}: {one_line(x['summary'], 70)}" for x in rows[::-1])

    def recent_lines(self, room: str, skip_id: int, n: int = RECENT_LINES) -> str:
        """The room's last n messages other than #skip_id (the one being answered) as one-line summaries,
        oldest first: what an answering model needs to not treat every message as the first (the room
        summary lags by up to summary_every). Includes answers posted after skip_id, e.g. by another agent."""
        rows = self.store.q("SELECT * FROM messages WHERE room=? AND id!=? ORDER BY id DESC LIMIT ?",
                            (room, skip_id, n))
        out = []
        for m in rows[::-1]:
            rec = _recips(m)
            out.append(f"#{m['id']} {m['sender']}" + (">" + ",".join(rec) if rec else "")
                       + (f" re#{m['reply_to']}" if m["reply_to"] else "") + f": {one_line(m['summary'], 200)}")
        return "\n".join(out)

    def duplicate_of(self, m) -> int | None:
        """Id of an earlier, already answered message with the same text from the same human in the same
        room (a double send), so it isn't answered from scratch a second time."""
        if not m["human"]:
            return None
        text = " ".join((text_of(m) or "").split())
        for p in self.store.q("SELECT * FROM messages WHERE room=? AND sender=? AND id<? AND created>? "
                              "ORDER BY id DESC LIMIT 5", (m["room"], m["sender"], m["id"], m["created"] - DUPLICATE_WINDOW)):
            if " ".join((text_of(p) or "").split()) == text and \
                    self.store.one("SELECT 1 FROM messages WHERE reply_to=? "
                                   "AND COALESCE(body, summary) NOT LIKE '(%; not answered)'", (p["id"],)):
                # Only the answer to the first copy in between: a double send (or a resend after a send that
                # looked failed). Anything else posted in between makes it a deliberate nudge ("see above"
                # again after more was said), which gets answered.
                between = self.store.one("SELECT 1 FROM messages WHERE room=? AND id>? AND id<? AND sender!=? "
                                         "AND reply_to IS NOT ? LIMIT 1", (m["room"], p["id"], m["id"], m["sender"], p["id"]))
                return None if between else p["id"]
        return None

    # ------------------------------------------------------------- bot mode
    def bot(self, room: str) -> bool:
        r = self.store.room(room)
        return bool(r and r["bot"])

    def room_note(self, room: str) -> str:
        """What every answer prompt in this room starts with ("" outside bot mode)."""
        return BOT_NOTE if self.bot(room) else ""

    def baton(self, m) -> str | None:
        """The one agent a bot-mode message hands work to: the first one it asks or opens a line with."""
        return self._baton(text_of(m) or "", m["sender"])

    def _baton(self, text: str, sender: str) -> str | None:
        text = text.lower()
        groups = sorted([(g.start(1), g.group(1)) for g in ADDRESS_RE.finditer(text)] +
                        [(g.start(1), g.group(1)) for g in LINE_AT_RE.finditer(text)])
        for _, group in groups:
            for n in MENTION_RE.findall(group):
                n = n.rstrip(".")
                if n != sender and n != HUB_SENDER and self.store.agent(n):
                    return n
        return None

    def routed(self, m) -> bool:
        """A new bot-mode message from an agent that names no agent: the router picked who it asks."""
        text = (text_of(m) or "").lower()
        return not m["human"] and m["reply_to"] is None and self.bot(m["room"]) and \
            not any(self.store.agent(n.rstrip(".")) for n in MENTION_RE.findall(text))

    def stall(self, m) -> str | None:
        """Why a bot-mode handoff in m would be a loop (None when it's fine): it repeats a recent
        message, or the last PING_PONG agent messages are just two agents taking turns."""
        cache = self._shingle_cache.setdefault(m["room"], OrderedDict())
        self._shingle_cache.move_to_end(m["room"])
        while len(self._shingle_cache) > 32:
            self._shingle_cache.popitem(last=False)

        def shingles(row):
            key = (row["id"], text_of(row))
            if key not in cache:
                cache[key] = _shingles(key[1])
            cache.move_to_end(key)
            while len(cache) > ECHO_LOOKBACK + 1:
                cache.popitem(last=False)
            return cache[key]

        sa = shingles(m)
        for p in self.store.q("SELECT * FROM messages WHERE room=? AND id<? AND sender!=? ORDER BY id DESC LIMIT ?",
                              (m["room"], m["id"], HUB_SENDER, ECHO_LOOKBACK)):
            sb = shingles(p)
            if sa and sb and len(sa & sb) / len(sa | sb) >= ECHO_SIMILARITY:
                return f"#{m['id']} repeats #{p['id']}"
        last = self.store.q("SELECT sender, human FROM messages WHERE room=? AND id<=? AND sender!=? "
                            "ORDER BY id DESC LIMIT ?", (m["room"], m["id"], HUB_SENDER, PING_PONG))
        senders = {r["sender"] for r in last}
        turns = all(a["sender"] != b["sender"] for a, b in zip(last, last[1:]))  # not one agent asking again and again
        if len(last) == PING_PONG and not any(r["human"] for r in last) and len(senders) == 2 and turns:
            a, b = sorted(senders)
            return f"{a} and {b} have passed it back and forth {PING_PONG} times"
        return None

    def _bot_turn(self, mid: int) -> None:
        """After an agent posts in a bot room: [done] ends bot mode; a looping handoff gets a note."""
        m = self.store.message(mid)
        if DONE_RE.search(text_of(m)):
            self.store.set_room_bot(m["room"], False)
            self.broadcast({"type": "rooms"})
            return
        why = self.stall(m) if self.baton(m) else None
        if why:
            try:
                self.post(HUB_SENDER, m["room"], f"(bot mode: {why}, so nobody was asked; "
                                                 "say what's next or post [done])", reply_to=mid)
            except HubError:
                pass  # rate limit: the note is a courtesy

    def wakes(self, m, agent: str) -> bool:
        """Whether agent-written message m should get an answer from agent. In bot mode: agent holds
        the baton (or was routed a new message that asks nobody, like a goal from an agent outside the
        run) and the handoff isn't a loop. Otherwise: it must ask the agent explicitly, the agent
        chain below the human must be short, and the agent must not already have been asked by that
        human (it answers the human, not a second time via another agent)."""
        if self.bot(m["room"]):
            asked = self.baton(m) == agent or (self.routed(m) and agent in _recips(m))
            return asked and not self.stall(m)
        if not addresses(text_of(m), agent) or self.agent_hops(m) >= MAX_AGENT_HOPS:
            return False
        root = m
        while root and not root["human"]:
            root = self.store.message(root["reply_to"]) if root["reply_to"] else None
        return not (root and agent in _recips(root))

    def turn_blocker(self, m, agent: str) -> str | None:
        """Agents addressed in the same message answer one after another, local models first (they're
        quick), so each answer can build on the ones before it. Returns who agent is still waiting for,
        or None when it's agent's turn. Agents that can't answer now (offline, cooling, manual) are skipped."""
        if time.time() - m["created"] > TURN_WAIT:
            return None
        live = []
        for n in _recips(m):
            if not m["human"] and n != agent and not self.wakes(m, n):
                continue  # only named in passing: it won't answer, so don't wait for it
            row = self.store.agent(n)
            state = self.worker_state(row) if row else ""
            if state in ("auto", "online", "busy"):
                live.append((state != "auto", n))
        order = [n for _, n in sorted(live, key=lambda x: x[0])]  # stable: keeps the addressed order
        if agent not in order:
            return None
        for n in order[:order.index(agent)]:
            if not self.store.one("SELECT 1 FROM messages WHERE reply_to=? AND sender=?", (m["id"], n)):
                return n
        return None

    async def wait_turn(self, m, agent: str) -> None:
        """Waits until turn_blocker lets agent answer m (for auto agents, which answer in-process)."""
        while self.turn_blocker(m, agent):
            await self.wait_for_new(5)

    def agent_hops(self, m) -> int:
        """Agent messages in m's reply chain since the last human message (m included)."""
        hops, seen = 0, 0
        while m and not m["human"] and seen < 20:
            hops += 1
            seen += 1
            m = self.store.message(m["reply_to"]) if m["reply_to"] else None
        return hops

    # ---------------------------------------------------------------- search
    def search(self, q: str, room: str | None = None, limit: int = 50) -> list[dict]:
        """Viewer search: ranked literal phrases, or substring search when SQLite lacks FTS5."""
        q = " ".join((q or "").split())
        if len(q) < 2:
            raise HubError("search for at least 2 characters")
        if self.store.fts:
            match = '"' + q.replace('"', '""') + '"'
            where = "messages_fts MATCH ?"
            fargs = [match]
            if room:
                where += " AND m.room=?"
                fargs.append(room.strip().lower().lstrip("#"))
            rows = self.store.q(
                "SELECT m.*, snippet(messages_fts, -1, '', '', '…', 32) AS excerpt "
                "FROM messages_fts JOIN messages m ON m.id=messages_fts.rowid "
                f"WHERE {where} ORDER BY bm25(messages_fts), m.id DESC LIMIT ?",
                (*fargs, max(1, min(int(limit), 200))))
            out = []
            for m in rows:
                j = self.message_json(m)
                j.pop("body")
                j.pop("size")
                j["snippet"] = " ".join(m["excerpt"].split())
                out.append(j)
            return out
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "(summary LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
        largs: list = [like, like]
        if room:
            where += " AND room=?"
            largs.append(room.strip().lower().lstrip("#"))
        rows = self.store.q(f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
                            (*largs, max(1, min(int(limit), 200))))
        out = []
        for m in rows:
            j = self.message_json(m)
            j.pop("body")
            j.pop("size")
            body = m["body"]  # bodies can be huge; send a line around the match instead
            j["snippet"] = j["summary"] if not body or q.lower() in j["summary"].lower() else _snippet(body, q)
            out.append(j)
        return out

    # ------------------------------------------------------------------- get
    def get(self, agent: str, mid: int, offset: int = 0) -> str:
        m = self.store.message(int(mid))
        if not m:
            raise HubError(f"no message #{mid}")
        text = text_of(m)
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
    def room_overview(self, agent: str = "", fallback: bool = False) -> list[dict]:
        # Aggregate once for the room lists; empty rooms still have zero counts.
        rows = self.store.q(
            "SELECT r.*, COUNT(m.id) AS count, COALESCE(MAX(m.id),0) AS last_id, "
            "COUNT(CASE WHEN m.id>r.summary_upto THEN 1 END) AS newer, "
            "COUNT(CASE WHEN m.id>COALESCE(c.last_id,0) AND m.sender!=? "
            "AND instr(m.recipients,?)>0 THEN 1 END) AS mine, "
            "COUNT(CASE WHEN m.id>COALESCE(c.last_id,0) AND m.sender!=? "
            "AND (m.recipients='' OR instr(m.recipients,?)>0) THEN 1 END) AS new "
            "FROM rooms r LEFT JOIN messages m ON m.room=r.name "
            "LEFT JOIN cursors c ON c.room=r.name AND c.agent=? GROUP BY r.name ORDER BY r.name",
            (agent, f",{agent},", agent, f",{agent},", agent))
        recent: dict[str, list[str]] = {}
        if fallback:
            for m in self.store.q(
                "SELECT * FROM (SELECT room,sender,summary,id, "
                "ROW_NUMBER() OVER (PARTITION BY room ORDER BY id DESC) AS rn FROM messages) "
                "WHERE rn<=5 ORDER BY id"):
                recent.setdefault(m["room"], []).append(f"{m['sender']}: {one_line(m['summary'], 70)}")
        paused = set()
        if not fallback and self.s.max_chain > 0:
            paused = {r["room"] for r in self.store.q(
                "SELECT room FROM (SELECT room,human,ROW_NUMBER() OVER "
                "(PARTITION BY room ORDER BY id DESC) AS rn FROM messages "
                "WHERE human=1 OR COALESCE(body,summary) NOT LIKE '(%; not answered)') "
                "WHERE rn<=? GROUP BY room HAVING COUNT(*)>=? AND SUM(human)=0",
                (self.s.max_chain, self.s.max_chain))}
        out = []
        for row in rows:
            r = dict(row)
            if r["summary"]:
                r["summary"] += f" (+{r['newer']} newer)" if r["newer"] else ""
            elif fallback:
                r["summary"] = "recent: " + " | ".join(recent[r["name"]]) if r["name"] in recent else "(empty)"
            r["paused"] = not r["bot"] and r["name"] in paused
            out.append(r)
        return out

    def rooms(self, agent: str) -> str:
        st = self.store
        joined = st.joined_rooms(agent)
        lines = []
        for r in self.room_overview(agent, fallback=True)[:50]:
            name = r["name"]
            mine = r["mine"]
            if name in joined:
                new = r["new"]
                status = f"{new} new" + (f", {mine} for you" if mine else "")
            else:
                status = "not joined" + (f", {mine} for you" if mine else "")
            if r["bot"]:
                status += ", bot mode"
            lines.append(f"{name} ({status}): {one_line(r['summary'], 100)}")
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

    def spent(self, agent: str) -> int:
        """Tokens counted against the daily budget: gateway asks plus a worker's own CLI runs,
        with cache reads weighted like providers bill them."""
        u = self.store.usage_today(agent)
        return u["ask"] + u["sub"] + int(u["cached"] * CACHE_WEIGHT)

    def over_budget(self, agent: str, budget: int | None = None, used: int | None = None) -> str | None:
        """Why agent can't spend more tokens today, or None when it can. Pass budget and used when
        they're already looked up (two database reads)."""
        if budget is None or used is None:
            budget, used = self.budget(agent), self.spent(agent)
        return f"daily token budget used ({used}/{budget}); resets 00:00 UTC" if budget and used >= budget else None

    # ---------------------------------------------------------------- worker
    # An agent can live in one of three places: on the hub (a gateway model: `model` set), on a
    # machine (clients/worker.py runs a CLI there: `worker` set), or nowhere we manage (it connects
    # itself with its own key, e.g. an interactive Claude Code session).
    @staticmethod
    def worker_config(row) -> dict:
        """How the machine should run this agent. {"mode": "manual"} = not on a machine."""
        try:
            cfg = json.loads(row["worker"]) if row and row["worker"] else None
        except ValueError:
            cfg = None
        if not cfg or not cfg.get("machine"):
            return {"mode": "manual"}
        return {**cfg, "system": row["system"] or ""}  # persona, shared with hub agents

    def runs_as(self, row, machines=None) -> str | None:
        """The power a machine agent actually gets: its setting, capped by the machine's WORKER_MAX_POWER and
        WORKER_DIRS (as clients/worker.py's effective_power does). None when it isn't on a machine."""
        cfg = self.worker_config(row)
        m = (self.store.machine(cfg.get("machine") or "") if machines is None else machines.get(cfg.get("machine"))) \
            if cfg.get("mode") == "worker" or cfg.get("machine") else None
        if not m:
            return None
        want = cfg.get("power") if cfg.get("power") in WORKER_POWERS else "answer"
        cap = m["max_power"] if m["max_power"] in WORKER_POWERS else "answer"
        power = min(want, cap, key=WORKER_POWERS.index)
        d = posixpath.normpath(cfg.get("dir") or "")
        dirs = json.loads(m["dirs"] or "[]")
        if power != "answer" and d.startswith("/") and not any(d == a or d.startswith(a.rstrip("/") + "/") for a in dirs):
            power = "answer"  # folder not allowed there (a ~/ path is only known on the machine: trust it)
        if power != "answer" and not dirs:
            power = "answer"
        return power

    def machine_agents(self, machine: str) -> list:
        return [a for a in self.store.agents()
                if a["kind"] == "agent" and not a["model"] and self.worker_config(a).get("machine") == machine]

    @staticmethod
    def machine_online(m) -> bool:
        return bool(m) and (m["last_seen"] or 0) > time.time() - WORKER_OFFLINE_AFTER

    def worker_state(self, row, machines=None) -> str:
        """auto / manual / offline / online / busy / cooling, as the viewer and router see it."""
        if row["kind"] != "agent":
            return ""
        if row["model"]:
            m = self.model_machine(row["model"], machines)
            return "offline" if m and not self.machine_online(self.store.machine(m) if machines is None else machines.get(m)) else "auto"
        cfg = self.worker_config(row)
        if cfg["mode"] != "worker":
            return "manual"
        if not self.machine_online(self.store.machine(cfg["machine"]) if machines is None else machines.get(cfg["machine"])):
            return "offline"
        if row["status"] == "cooling":
            return "cooling" if (row["status_until"] or 0) > time.time() else "online"
        return row["status"] or "online"

    def clean_worker(self, cfg: dict | None) -> dict | None:
        """Validates a worker config from the viewer; None = not on a machine."""
        if not cfg or cfg.get("mode", "manual") == "manual":
            return None
        m = self.store.machine((cfg.get("machine") or "").strip())
        if not m:
            raise HubError("pick a machine (add one under Machines first)")
        cli = cfg.get("cli")
        cli = "agy" if cli == "gemini" else cli
        if cli not in WORKER_CLIS:
            raise HubError(f"cli must be one of {', '.join(WORKER_CLIS)}")
        found = [c["cli"] for c in json.loads(m["clis"] or "[]")]
        if found and cli not in found:
            raise HubError(f"{m['name']} doesn't have {cli} (it has {', '.join(found)})")
        power = cfg.get("power") or "answer"
        if power not in WORKER_POWERS:
            raise HubError(f"power must be one of {', '.join(WORKER_POWERS)}")
        # No folder of its own is fine: the agent then uses its power only in rooms that have a shared folder (#84).

        def num(key, default, lo, hi):
            try:
                return max(lo, min(hi, int(cfg.get(key) or default)))
            except (TypeError, ValueError):
                raise HubError(f"{key} must be a number") from None
        return {
            "mode": "worker", "machine": m["name"], "cli": cli, "power": power,
            "model": (cfg.get("model") or "").strip()[:64],
            "deep_model": (cfg.get("deep_model") or "").strip()[:64],
            "dir": (cfg.get("dir") or "").strip()[:512] if power != "answer" else "",
            "max_output": num("max_output", 800, 50, 8000),
            "max_input_chars": num("max_input_chars", 12000, 1000, 100_000),
        }

    async def refresh_model_hosts(self) -> None:
        if not self.llm.enabled or time.time() - self._hosts_at < 300:
            return
        self._hosts_at = time.time()
        try:
            self._hosts = await self.llm.model_hosts()
        except Exception:  # older LiteLLM / key without access: just don't show hosts
            self._hosts = {}

    def model_machine(self, model: str | None, machines=None) -> str | None:
        """The paired machine a gateway model runs on (its api_base points there), if any."""
        host = (self._hosts.get(model or "") or "").lower()
        if not host:
            return None
        host = urlsplit(f"//{host}").hostname or host
        short = host if host[:1].isdigit() or ":" in host else host.split(".")[0]  # workstation.example.ts.net -> workstation
        for m in (self.store.machines() if machines is None else machines.values()):
            names = {m["name"], (m["hostname"] or "").lower(), *(a.lower() for a in json.loads(m["addresses"] or "[]"))}
            if host in names or short in names:
                return m["name"]
        return None

    def model_host(self, model: str | None, machines=None) -> str | None:
        """Where a gateway model runs: a paired machine's name if it's one of ours, else the raw host."""
        return self.model_machine(model, machines) or self._hosts.get(model or "")

    async def model_list(self) -> str:
        names = await self.llm.models()
        plain = "models: " + ", ".join(names)
        try:
            info = await self.llm.model_info()
        except (LLMError, AttributeError):  # older gateways / restricted keys still list names
            return plain
        if not info:
            return plain
        self._hosts = await self.llm.model_hosts(info)
        self._hosts_at = time.time()
        hosts = sorted({self._hosts[n] for n in names if n in self._hosts}) if self.llm.model_probe else []
        status = dict(zip(hosts, await asyncio.gather(*(self.llm._reachable(h) for h in hosts))))
        details = {m["name"]: m for m in info}
        lines = []
        for name in names:
            m = details.get(name, {})
            host = self._hosts.get(name)
            parts = [self.model_host(name)] if host else (["paid API"] if m and not m.get("api_base") else [])
            if host in status:
                parts.append("up" if status[host] else "DOWN: not reachable from the hub")
            if m.get("context"):
                parts.append(f"context {m['context']}")
            if m.get("description"):
                parts.append(" ".join(m["description"].split()))
            lines.append(name + (" [" + ", ".join(parts) + "]" if parts else ""))  # type: ignore[arg-type]  # model_host is a str whenever host is set
        return "models:\n" + "\n".join(lines)

    def agent_machine(self, row, machines=None) -> str | None:
        """The computer that actually answers for an agent: its worker's machine, or where its model runs."""
        if row["kind"] != "agent":
            return None
        if row["model"]:
            return self.model_machine(row["model"], machines)
        return self.worker_config(row).get("machine")

    def _unavailable_notices(self, mid: int) -> None:
        """A message to an agent whose machine is off (or out of quota) gets an answer right away
        saying so, instead of silence. That reply also marks it handled: the machine skips it later."""
        m = self.store.message(mid)
        for name in _recips(m):
            row = self.store.agent(name)
            if not row or row["kind"] != "agent":
                continue
            state = self.worker_state(row)
            cfg = self.worker_config(row)
            if state == "offline":
                what = row["model"] or cfg["cli"]
                note = f"({self.agent_machine(row)} is offline, so {what} can't answer; not answered)"
            elif state == "cooling":
                until = time.strftime("%H:%M UTC", time.gmtime(row["status_until"]))  # readers are in other zones
                note = f"(out of {cfg['cli']} quota on {cfg['machine']} until {until}; not answered)"
            else:
                continue
            try:
                self._not_answered(name, m, note)
            except Exception:
                pass  # the notice is a courtesy

    def worker_report(self, agent: str, status: str, tokens: int = 0, calls: int = 0,
                      until: float | None = None, room: str | None = None, cached: int = 0) -> str:
        """A worker says what it's doing and what its CLI spent. Returns the budget state in one line."""
        if status not in WORKER_STATUSES:
            raise HubError(f"status must be one of {', '.join(WORKER_STATUSES)}")
        st = self.store
        if tokens or calls or cached:
            st.add_usage(agent, sub=max(0, int(tokens)), calls=max(0, int(calls)), cached=max(0, int(cached)))
        st.set_status(agent, status, until if status == "cooling" else None)
        if room:  # viewer shows "thinking…" while the CLI runs
            self.broadcast({"type": "typing", "agent": agent, "room": room, **({} if status == "busy" else {"done": True})})
        self.broadcast({"type": "agents"})
        budget, used = self.budget(agent), self.spent(agent)
        if budget and used >= budget:
            return f"over budget {used}/{budget}"
        return f"ok {used}/{budget or 'unlimited'}"

    async def ask(self, agent: str, model: str, prompt: str | None = None, system: str | None = None,
                  max_tokens: int | None = None, room: str | None = None, raw: bool = False,
                  note: str | None = None, empty_notice: Callable[[str], None] | None = None,
                  format: str = "text") -> str:
        """raw: return just the model's text ("" when it said nothing), for the hub's own callers."""
        if format not in ("text", "code"):
            raise HubError("format must be 'text' or 'code'")
        if note is not None and (not isinstance(note, str) or len(note) > 200):
            raise HubError("note must be at most 200 characters")
        note = " ".join((note or "").split())
        if note and not room:
            raise HubError("note requires room")
        empty: list[str] = []
        if not self.llm.enabled:
            raise HubError("no model gateway configured (LITELLM_URL)")
        model = (model or "").strip()
        try:
            if model in ("", "?"):
                return self._charge_ctx(agent, await self.model_list())
            if not prompt:
                raise HubError("prompt required")
            budget, used = self.budget(agent), self.spent(agent)
            why = self.over_budget(agent, budget, used)
            if why:
                raise HubError(why)
            requested = int(max_tokens) if max_tokens is not None else self.s.ask_max_tokens
            limit = self.s.ask_max_tokens_limit
            remaining = max(0, budget - used) if budget else None
            mt = max(1, min(requested, limit))
            reason = "the hub's limit"
            if remaining is not None and remaining < mt:
                mt, reason = remaining, "what is left of today's budget"
            lowered = (f"\n[max_tokens lowered from {requested} to {mt}: {reason}]"
                       if not raw and max_tokens is not None and mt < requested else "")
            cut_notes = []
            def cut_notice(n):
                if raw:  # auto agents keep the original answer text, including its cut-off marker
                    return "…(cut off at the length limit; ask me to continue)"
                notice = (f"…(cut off at max_tokens={n}; ask again with a higher max_tokens, "
                          f"up to {limit} and within today's remaining budget, or ask for less)")
                cut_notes.append(notice)
                return notice
            msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
            # the retries inside chat() stop once today's budget is used up
            text, tokens = await self.llm.chat(model, msgs, mt, user=agent,
                                               remaining=remaining, retry_limit=limit, cut_notice=cut_notice,
                                               empty_notice=empty.append)
        except LLMError as e:
            raise HubError(str(e)) from e
        self.store.add_usage(agent, ask=tokens)
        used += tokens
        why = empty[0] if empty else "the model returned nothing"
        if not text and empty_notice:
            empty_notice(why)
        if raw:
            return self._charge_ctx(agent, text or "")
        answer = text or f"(empty answer: {why})"
        budget_note = f"\n[budget {used}/{budget} today]" if budget and used > 0.8 * budget else ""
        if room:  # put the answer in a room for visibility; hand back only the short line
            res = self.post(agent, room, (note + "\n" if note else "") + answer + lowered)
            mid = post_id(res)
            return self._charge_ctx(agent, f"posted {self.fmt_line(self.store.message(mid), agent, True)}{budget_note}{lowered}")
        if format == "code":
            cut = cut_notes[-1] if cut_notes else ""
            cut_note = "\n" + cut if cut and answer.endswith(cut) else ""
            original = answer[:-len(cut)].rstrip("\r\n") if cut_note else answer
            fence = re.search(r"(?m)^ {0,3}(`{3,}|~{3,})[^\r\n]*\r?\n", original)
            if fence:
                code = original[fence.end():]
                closing = re.search(r"(?m)^ {0,3}" + fence[1][0] + "{" + str(len(fence[1]))
                                    + r",}[ \t]*\r?$", code)
                answer = (code[:closing.start()] if closing else code) + cut_note
        return self._charge_ctx(agent, answer + budget_note + lowered)
